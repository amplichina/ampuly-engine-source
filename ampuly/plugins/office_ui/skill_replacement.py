"""Versioned, inert replacement candidates for an existing internal Skill.

This module never edits the shared Skill library.  It fingerprints the exact
current directory, validates a narrowly structured replacement, and writes a
draft bundle with an unrun test record and a human-readable diff.  Admission
and atomic replacement are deliberately separate future gates.

Lifecycle (4.7-A03): draft -> test_passed | test_failed (a person's verdict);
a test_passed candidate then binds its frozen dual-run spec and receives the
machine comparison; only a passed comparison of *this* candidate's exact
SKILL.md may be approved.  A failed test or a failed comparison is final for
that candidate: the fixed text becomes a new draft (new fingerprint) that
names the one it revises.  Every transition appends a reason to the
manifest's ``transitions``.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ampuly.core.config import replace_with_retry
from ampuly.plugins.office_ui.rsi_evaluation import (
    FrozenEvaluationSpec,
    compare_evaluation_runs,
    freeze_evaluation_spec,
)
from ampuly.plugins.office_ui.workflow_packages import load_skill_contract


class SkillReplacementError(ValueError):
    pass


@dataclass(frozen=True)
class SkillReplacementCandidate:
    root: Path
    manifest: dict[str, Any]

    @property
    def candidate_id(self) -> str:
        return str(self.manifest.get("candidate_id", ""))

    @property
    def fingerprint(self) -> str:
        return str(self.manifest.get("candidate_fingerprint", ""))

    @property
    def status(self) -> str:
        return str(self.manifest.get("status", ""))

    def to_payload(self) -> dict[str, Any]:
        """Console-safe review facts; never expose local roots or review notes."""

        return {
            "candidate_id": self.candidate_id,
            "candidate_fingerprint": self.fingerprint,
            "skill_id": str(self.manifest.get("skill_id", "")),
            "version": str(self.manifest.get("version", "")),
            "current_version": str(self.manifest.get("current_version", "")),
            "status": self.status,
            "created_at": str(self.manifest.get("created_at", "")),
            "tested_at": str(self.manifest.get("tested_at", "")),
            "reviewed_at": str(self.manifest.get("reviewed_at", "")),
            "evaluation_spec_fingerprint": str(
                self.manifest.get("evaluation_spec_fingerprint", "")
            ),
            "evaluation_bound": bool(self.manifest.get("evaluation_report_sha256")),
            # 4.7-A03: where the dual run stands and, in the owner's words, why approval is not open yet.
            "evaluation_spec_bound": bool(self.manifest.get("evaluation_spec_fingerprint")),
            "evaluation_standard": evaluation_standard(self),
            "evaluation_verdict": evaluation_verdict(self),
            "evaluation_summary": evaluation_summary(self),
            "approval_blocker": skill_replacement_approval_blocker(self),
            "revises": _link_id(self.manifest.get("revises")),
            "superseded_by": _link_id(self.manifest.get("superseded_by")),
            "history": [
                {key: str(entry.get(key, "") or "") for key in ("at", "actor", "action", "from", "to", "reason")}
                for entry in self.manifest.get("transitions", []) or []
                if isinstance(entry, dict)
            ],
        }


_SKILL_ID = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_KEYS = frozenset({"skill_id", "version", "description", "instructions", "success_criteria", "test_plan"})
_SECRET_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9]{12,}"),
    re.compile(r"\bAKIA[A-Z0-9]{12,}"),
    re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}", re.IGNORECASE),
)
_MAX_FILES = 500
_MAX_BYTES = 20 * 1024 * 1024
_CANDIDATE_ID = re.compile(r"^[a-z][a-z0-9_-]{1,175}-[a-f0-9]{16}$")
_LIFECYCLE = frozenset({"draft", "test_passed", "test_failed", "approved", "rejected"})
# The front-matter fields a replacement sets; every other field of the current skill is carried over.
_CONTRACT_FIELDS = ("version", "status", "provenance", "tests", "success_criteria")
# 4.7-A03: what the owner reads about a dual run. Metric keys stay machine identifiers; these are their names.
_METRIC_WORDS = {
    "completion_rate": "完成率",
    "artifact_rate": "交出产物的比例",
    "failure_rate": "失败率",
    "human_pause_rate": "停下来等人的比例",
    "acceptance_rate": "验收通过率",
    "delivery_hours": "交付用时（小时）",
}
_FLAG_WORDS = {
    "same_run_used_for_baseline_and_candidate": "新旧两版用的是同一次运行",
    "same_evidence_used_for_baseline_and_candidate": "新旧两版用的是同一份运行证据",
}
_WRONG_ASSET = "评估标准测的不是这个候选：评估对象的指纹和当前候选对不上。"
_EVALUATED_ONCE = "这个候选已经做过一次双跑评估，结果不能重做；没通过就修订成新候选再测。"


def _front_matter(text: str) -> dict[str, Any]:
    """The YAML front matter of a SKILL.md, or {} when there is none that parses to a mapping."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        loaded = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def project_skill_candidate_root(ampuly_home: Path, project_id: str) -> Path:
    project = _text(project_id, "project_id", 256)
    key = hashlib.sha256(project.encode("utf-8")).hexdigest()[:24]
    return Path(ampuly_home).resolve() / "skill_candidates" / "projects" / key


def skill_tree_fingerprint(skill_root: Path) -> str:
    """Hash every real file and relative path in one bounded Skill directory."""

    supplied = Path(skill_root)
    if not supplied.is_dir() or _linked(supplied):
        raise SkillReplacementError("current skill is not a real directory")
    root = supplied.resolve()
    rows: list[tuple[str, str]] = []
    total = 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        base = Path(folder)
        for name in list(dirs):
            path = base / name
            if _linked(path):
                raise SkillReplacementError("current skill contains a linked directory")
        for name in files:
            path = base / name
            if _linked(path) or not path.is_file():
                raise SkillReplacementError("current skill contains a linked or non-file entry")
            total += path.stat().st_size
            if len(rows) >= _MAX_FILES or total > _MAX_BYTES:
                raise SkillReplacementError("current skill exceeds the bounded candidate limit")
            rows.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    if not rows or not (root / "SKILL.md").is_file():
        raise SkillReplacementError("current skill has no SKILL.md")
    return hashlib.sha256(_canonical(rows)).hexdigest()


def normalize_skill_replacement(payload: dict[str, Any], *, current_version: str = "") -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _KEYS:
        raise SkillReplacementError("skill replacement fields differ from the supported contract")
    skill_id = _text(payload.get("skill_id"), "skill_id", 64)
    if not _SKILL_ID.fullmatch(skill_id):
        raise SkillReplacementError("skill_id is invalid")
    version = _text(payload.get("version"), "version", 32)
    if not _VERSION.fullmatch(version):
        raise SkillReplacementError("version must be semantic x.y.z")
    if current_version:
        if not _VERSION.fullmatch(str(current_version)):
            raise SkillReplacementError("current skill version is not semantic x.y.z")
        if tuple(map(int, version.split("."))) <= tuple(map(int, str(current_version).split("."))):
            raise SkillReplacementError("replacement version must be greater than the current version")
    description = _text(payload.get("description"), "description", 300)
    instructions = _text(payload.get("instructions"), "instructions", 20000)
    criteria = _text_list(payload.get("success_criteria"), "success_criteria")
    tests = _text_list(payload.get("test_plan"), "test_plan")
    _no_secrets([description, instructions, *criteria, *tests])
    return {
        "skill_id": skill_id,
        "version": version,
        "description": description,
        "instructions": instructions,
        "success_criteria": criteria,
        "test_plan": tests,
    }


def create_skill_replacement_candidate(
    root: Path,
    current_skill_root: Path,
    payload: dict[str, Any],
    *,
    actor: str,
    expected_current_fingerprint: str,
    proposal_id: str,
    proposal_fingerprint: str,
) -> SkillReplacementCandidate:
    """Write an immutable draft bundle; never touch or replace the shared Skill."""

    actor = _text(actor, "actor", 128)
    current = load_skill_contract(Path(current_skill_root).parent, Path(current_skill_root).name)
    if not current.complete:
        raise SkillReplacementError("current skill release contract is incomplete: " + "; ".join(current.problems))
    observed = skill_tree_fingerprint(current_skill_root)
    if observed != str(expected_current_fingerprint or "").strip().lower():
        raise SkillReplacementError("target fingerprint changed; regenerate the improvement proposal")
    normalized = normalize_skill_replacement(payload, current_version=current.version)
    if normalized["skill_id"] != current.skill_id:
        raise SkillReplacementError("replacement skill_id does not match the current skill")
    clean_proposal_id = _text(proposal_id, "proposal_id", 192)
    clean_proposal_fingerprint = _sha(proposal_fingerprint, "proposal_fingerprint")
    candidate_identity = {
        "payload": normalized,
        "proposal_id": clean_proposal_id,
        "proposal_fingerprint": clean_proposal_fingerprint,
        "current_fingerprint": observed,
    }
    payload_fingerprint = hashlib.sha256(_canonical(candidate_identity)).hexdigest()
    payload_json = json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    candidate_id = f"{normalized['skill_id']}-{normalized['version'].replace('.', '_')}-{payload_fingerprint[:16]}"
    base = Path(root).resolve()
    base.mkdir(parents=True, exist_ok=True)
    target = (base / candidate_id).resolve()
    if target.parent != base:
        raise SkillReplacementError("candidate path escaped its store")
    if target.exists():
        return load_skill_replacement_candidate(target)

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old_text = Path(current_skill_root, "SKILL.md").read_text(encoding="utf-8")
    # The replacement changes the contract fields and the text, nothing else: whatever else the current front matter
    # says (a display name, a licence, what it requires, its own notes) is carried over, and its provenance is kept
    # as where this one came from. The contract fields go under metadata only -- a top-level copy would be read
    # first and would keep the old version (workflow_packages._frontmatter_field).
    current_front = _front_matter(old_text)
    kept_top = {key: value for key, value in current_front.items() if key not in {"name", "description", "metadata", *_CONTRACT_FIELDS}}
    current_nested = current_front.get("metadata") if isinstance(current_front.get("metadata"), dict) else {}
    kept_nested = {key: value for key, value in current_nested.items() if key not in _CONTRACT_FIELDS}
    previous_provenance = current_front.get("provenance", current_nested.get("provenance"))
    provenance: dict[str, Any] = {
        "source": "superones_improvement_proposal",
        "proposal_id": clean_proposal_id,
        "proposal_fingerprint": clean_proposal_fingerprint,
        "replaces_version": current.version,
        "replaces_fingerprint": observed,
        "generated_at": stamp,
        "reviewed_at": "",
        "reviewed_by": "",
    }
    if isinstance(previous_provenance, dict) and previous_provenance:
        provenance["previous"] = previous_provenance
    meta = {
        "name": normalized["skill_id"],
        "description": normalized["description"],
        **kept_top,
        "metadata": {
            **kept_nested,
            "version": normalized["version"],
            "status": "draft",
            "provenance": provenance,
            "tests": ["static_frontmatter_contract", "tests/contract.md"],
            "success_criteria": normalized["success_criteria"],
        },
    }
    skill_md = f"---\n{yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()}\n---\n\n{normalized['instructions'].rstrip()}\n"
    test_meta = {"date": stamp[:10], "outcome": "unrun", "recorded_by": actor}
    contract_md = (
        f"---\n{yaml.safe_dump(test_meta, allow_unicode=True, sort_keys=False).strip()}\n---\n\n"
        "# 替代候选测试计划（尚未运行）\n\n"
        + "\n".join(f"- {item}" for item in normalized["test_plan"])
        + "\n"
    )
    diff = "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True), skill_md.splitlines(keepends=True),
        fromfile=f"{current.skill_id}@{current.version}/SKILL.md",
        tofile=f"{normalized['skill_id']}@{normalized['version']}/SKILL.md",
    ))
    manifest = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "candidate_fingerprint": payload_fingerprint,
        "status": "draft",
        "skill_id": normalized["skill_id"],
        "version": normalized["version"],
        "current_version": current.version,
        "current_fingerprint": observed,
        "created_at": stamp,
        "created_by": actor,
        "proposal_id": meta["metadata"]["provenance"]["proposal_id"],
        "proposal_fingerprint": meta["metadata"]["provenance"]["proposal_fingerprint"],
        "files_sha256": {
            "payload.json": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            "SKILL.md": hashlib.sha256(skill_md.encode("utf-8")).hexdigest(),
            "tests/contract.md": hashlib.sha256(contract_md.encode("utf-8")).hexdigest(),
            "DIFF.md": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        },
        "transitions": [{
            "at": stamp, "actor": actor, "action": "created", "from": "", "to": "draft",
            "reason": f"生成候选版本 v{normalized['version']}（替代 v{current.version}），等待测试",
        }],
    }
    staging = Path(tempfile.mkdtemp(prefix=f".{candidate_id}-", dir=base))
    try:
        (staging / "tests").mkdir()
        (staging / "payload.json").write_text(payload_json, encoding="utf-8", newline="\n")
        (staging / "SKILL.md").write_text(skill_md, encoding="utf-8", newline="\n")
        (staging / "tests" / "contract.md").write_text(contract_md, encoding="utf-8", newline="\n")
        (staging / "DIFF.md").write_text(diff, encoding="utf-8", newline="\n")
        (staging / "candidate.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        replace_with_retry(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return load_skill_replacement_candidate(target)


def load_skill_replacement_candidate(root: Path) -> SkillReplacementCandidate:
    supplied = Path(root)
    if not supplied.is_dir() or _linked(supplied):
        raise SkillReplacementError("skill candidate is not a real directory")
    root = supplied.resolve()
    try:
        manifest = json.loads((root / "candidate.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SkillReplacementError("skill candidate manifest is unreadable") from exc
    if manifest.get("status") not in _LIFECYCLE:
        raise SkillReplacementError("skill replacement candidate status is invalid")
    expected = dict(manifest.get("files_sha256", {}) or {})
    for relative in ("payload.json", "SKILL.md", "tests/contract.md", "DIFF.md"):
        path = root / relative
        if not path.is_file() or _linked(path) or hashlib.sha256(path.read_bytes()).hexdigest() != expected.get(relative):
            raise SkillReplacementError(f"skill candidate file changed: {relative}")
    try:
        normalized = json.loads((root / "payload.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SkillReplacementError("skill candidate payload is unreadable") from exc
    fingerprint = hashlib.sha256(_canonical({
        "payload": normalized,
        "proposal_id": str(manifest.get("proposal_id", "")),
        "proposal_fingerprint": str(manifest.get("proposal_fingerprint", "")),
        "current_fingerprint": str(manifest.get("current_fingerprint", "")),
    })).hexdigest()
    if fingerprint != str(manifest.get("candidate_fingerprint", "")):
        raise SkillReplacementError("skill candidate identity changed")
    expected_id = f"{normalized.get('skill_id', '')}-{str(normalized.get('version', '')).replace('.', '_')}-{fingerprint[:16]}"
    if root.name != expected_id or manifest.get("candidate_id") != expected_id:
        raise SkillReplacementError("skill candidate id does not match its payload")
    for field, relative in (
        ("evaluation_spec_sha256", "evaluation-spec.json"),
        ("evaluation_report_sha256", "evaluation-report.json"),
        ("test_record_sha256", "test-result.json"),
        ("review_sha256", "review.json"),
    ):
        digest = str(manifest.get(field, "") or "")
        path = root / relative
        if digest:
            if not path.is_file() or _linked(path) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise SkillReplacementError(f"skill candidate decision record changed: {relative}")
        elif path.exists():
            raise SkillReplacementError(f"skill candidate has an unbound decision record: {relative}")
    return SkillReplacementCandidate(root, manifest)


def skill_candidate_evaluation_asset(candidate: SkillReplacementCandidate) -> dict[str, str]:
    """What a skill candidate's dual run measures: this candidate's exact SKILL.md, and nothing beside it.

    The spec is frozen against this identity, the Harness sessions are bound to its digest before their first
    turn, and approval re-derives it from the candidate on disk -- so a comparison of anything else (another
    candidate, a workflow, an older revision) can never open this candidate's approval.
    """

    files = dict(candidate.manifest.get("files_sha256", {}) or {})
    return {
        "asset_kind": "skill",
        "asset_id": str(candidate.manifest.get("skill_id", "")),
        "version": str(candidate.manifest.get("version", "")),
        "candidate_fingerprint": candidate.fingerprint,
        "manifest_sha256": str(files.get("SKILL.md", "")),
    }


def skill_replacement_evaluation_spec(candidate: SkillReplacementCandidate) -> FrozenEvaluationSpec:
    """Load and verify the frozen dual-run spec bound to this candidate."""

    candidate = load_skill_replacement_candidate(candidate.root)
    spec_path = candidate.root / "evaluation-spec.json"
    if not spec_path.is_file():
        raise SkillReplacementError("skill candidate has no frozen evaluation spec")
    try:
        stored = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SkillReplacementError("skill candidate evaluation spec is unreadable") from exc
    if not isinstance(stored, dict):
        raise SkillReplacementError("skill candidate evaluation spec is invalid")
    supplied_fingerprint = str(stored.pop("spec_fingerprint", "") or "")
    if stored.pop("schema_version", None) != 1:
        raise SkillReplacementError("skill candidate evaluation spec version is unsupported")
    try:
        spec = freeze_evaluation_spec(stored)
    except ValueError as exc:
        raise SkillReplacementError(str(exc)) from exc
    if spec.fingerprint != supplied_fingerprint or spec.fingerprint != str(
        candidate.manifest.get("evaluation_spec_fingerprint", "")
    ):
        raise SkillReplacementError("skill candidate evaluation spec identity changed")
    return spec


def bind_skill_replacement_evaluation(
    candidate: SkillReplacementCandidate,
    spec: FrozenEvaluationSpec,
    *,
    actor: str = "system",
) -> SkillReplacementCandidate:
    """Freeze the predeclared evaluation beside a still-inert candidate.

    4.7-A03: allowed before or after the person's test verdict (draft or test_passed). The goalposts still
    cannot move after evidence exists: the spec is bound once, names this exact candidate, and every Harness
    sample must be bound to its fingerprint before its first turn runs.
    """

    candidate = load_skill_replacement_candidate(candidate.root)
    if candidate.status not in {"draft", "test_passed"}:
        raise SkillReplacementError("只有待测试或测试已通过的候选才能定双跑评估标准。")
    if spec.payload.get("asset") != skill_candidate_evaluation_asset(candidate):
        raise SkillReplacementError(_WRONG_ASSET)
    path = candidate.root / "evaluation-spec.json"
    if path.exists():
        if str(candidate.manifest.get("evaluation_spec_fingerprint", "")) != spec.fingerprint:
            raise SkillReplacementError("这个候选已经定过另一套评估标准：标准定下后不能改。")
        return candidate
    _atomic_json(path, spec.to_payload())
    manifest = dict(candidate.manifest)
    manifest.update({
        "evaluation_spec_fingerprint": spec.fingerprint,
        "evaluation_spec_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    })
    measures = "、".join(_metric_word(item["name"]) for item in spec.payload["metrics"])
    _transition(
        manifest, action="evaluation_spec_bound", actor=_text(actor, "actor", 128),
        before=candidate.status, after=candidate.status,
        reason=f"定下双跑评估标准：比较{measures}，每组至少 {spec.payload['min_samples']} 个样本",
    )
    _atomic_json(candidate.root / "candidate.json", manifest)
    return load_skill_replacement_candidate(candidate.root)


def record_skill_replacement_evaluation(
    candidate: SkillReplacementCandidate,
    *,
    baseline: dict[str, Any],
    candidate_run: dict[str, Any],
    actor: str,
    note: str,
) -> SkillReplacementCandidate:
    """Record a machine-verifiable dual run; never accept a prose self-grade.

    After a person's passed test (4.7-A03) the comparison is recorded beside it and the status stays
    test_passed: a failed comparison blocks approval instead. On a draft the comparison also stands as the
    test verdict (the original path). Either way it is recorded once: a second run to fish for a pass is refused.
    """

    candidate = load_skill_replacement_candidate(candidate.root)
    if candidate.status not in {"draft", "test_passed"}:
        raise SkillReplacementError("只有待测试或测试已通过的候选才能记录双跑评估。")
    if candidate.manifest.get("evaluation_report_sha256"):
        raise SkillReplacementError(_EVALUATED_ONCE)
    spec = skill_replacement_evaluation_spec(candidate)
    if spec.payload.get("asset") != skill_candidate_evaluation_asset(candidate):
        raise SkillReplacementError(_WRONG_ASSET)
    try:
        report = compare_evaluation_runs(spec, baseline, candidate_run)
    except ValueError as exc:
        raise SkillReplacementError(str(exc)) from exc
    record = {
        "candidate_fingerprint": candidate.fingerprint,
        "evaluation": report,
        "actor": _text(actor, "actor", 128),
        "note": _text(note, "note", 2000),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    report_path = candidate.root / "evaluation-report.json"
    _atomic_json(report_path, record)
    result = "passed" if report["verdict"] == "passed" else "failed"
    report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
    manifest = dict(candidate.manifest)
    manifest.update({
        "evaluation_report_sha256": report_sha,
        "evaluation_verdict": result,
        "evaluated_at": record["recorded_at"],
        "evaluated_by": record["actor"],
    })
    outcome = _evaluation_words(report)
    if candidate.status == "draft":
        test_record = {
            "candidate_fingerprint": candidate.fingerprint,
            "outcome": result,
            "actor": record["actor"],
            "note": record["note"],
            "recorded_at": record["recorded_at"],
            "evaluation_report_sha256": report_sha,
        }
        _atomic_json(candidate.root / "test-result.json", test_record)
        manifest.update({
            "status": f"test_{result}",
            "tested_at": record["recorded_at"],
            "tested_by": record["actor"],
            "test_record_sha256": hashlib.sha256((candidate.root / "test-result.json").read_bytes()).hexdigest(),
        })
        reason = f"{outcome}（同时作为测试结论）"
    else:
        reason = outcome
    _transition(
        manifest, action="evaluation_recorded", actor=record["actor"],
        before=candidate.status, after=str(manifest["status"]), reason=reason,
    )
    _atomic_json(candidate.root / "candidate.json", manifest)
    return load_skill_replacement_candidate(candidate.root)


def skill_replacement_by_id(root: Path, candidate_id: str) -> SkillReplacementCandidate:
    clean = str(candidate_id or "").strip()
    if not _CANDIDATE_ID.fullmatch(clean):
        raise SkillReplacementError("skill candidate id is invalid")
    base = Path(root).resolve()
    target = (base / clean).resolve()
    if target.parent != base or not target.is_dir() or _linked(target):
        raise SkillReplacementError("skill candidate does not exist")
    return load_skill_replacement_candidate(target)


def list_skill_replacement_candidates(root: Path) -> list[SkillReplacementCandidate]:
    """Load every visible candidate strictly; corrupt evidence must not disappear."""

    base = Path(root).resolve()
    if not base.exists():
        return []
    if not base.is_dir() or _linked(base):
        raise SkillReplacementError("skill candidate store is unsafe")
    candidates: list[SkillReplacementCandidate] = []
    for child in sorted(base.iterdir(), key=lambda item: item.name):
        if child.name.startswith("."):
            continue
        if not child.is_dir() or _linked(child):
            raise SkillReplacementError("skill candidate store contains an unsafe entry")
        candidates.append(load_skill_replacement_candidate(child))
    return sorted(
        candidates,
        key=lambda candidate: (
            str(candidate.manifest.get("created_at", "")), candidate.candidate_id,
        ),
        reverse=True,
    )


def record_skill_replacement_test(
    candidate: SkillReplacementCandidate,
    *,
    outcome: str,
    actor: str,
    note: str,
) -> SkillReplacementCandidate:
    """Record one bounded test verdict; no arbitrary test command is executed."""

    candidate = load_skill_replacement_candidate(candidate.root)
    if candidate.status != "draft":
        raise SkillReplacementError("only a draft skill candidate can receive its test verdict")
    result = str(outcome or "").strip().lower()
    if result not in {"passed", "failed"}:
        raise SkillReplacementError("skill candidate test outcome must be passed or failed")
    record = {
        "candidate_fingerprint": candidate.fingerprint,
        "outcome": result,
        "actor": _text(actor, "actor", 128),
        "note": _text(note, "note", 2000),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _atomic_json(candidate.root / "test-result.json", record)
    manifest = dict(candidate.manifest)
    manifest.update({
        "status": f"test_{result}",
        "tested_at": record["recorded_at"],
        "tested_by": record["actor"],
        "test_record_sha256": hashlib.sha256((candidate.root / "test-result.json").read_bytes()).hexdigest(),
    })
    _transition(
        manifest, action="tested", actor=record["actor"], before="draft", after=f"test_{result}",
        reason="测试结论：通过，下一步做双跑评估" if result == "passed" else "测试结论：没通过，要修订成新候选再测",
    )
    _atomic_json(candidate.root / "candidate.json", manifest)
    return load_skill_replacement_candidate(candidate.root)


def review_skill_replacement_candidate(
    candidate: SkillReplacementCandidate,
    *,
    approved: bool,
    actor: str,
    note: str,
    expected_fingerprint: str,
) -> SkillReplacementCandidate:
    """Bind an owner verdict to the exact tested candidate without installing it.

    4.7-A03: approval also needs a passed dual run whose evaluated object is this exact candidate
    (skill_replacement_approval_blocker); a refusal says why in the owner's words. Rejection needs a passed
    test only, as before.
    """

    candidate = load_skill_replacement_candidate(candidate.root)
    if candidate.fingerprint != str(expected_fingerprint or "").strip():
        raise SkillReplacementError("skill candidate fingerprint changed; reload before review")
    wanted = "approved" if approved else "rejected"
    if candidate.status in {"approved", "rejected"}:
        if candidate.status != wanted:
            raise SkillReplacementError("skill candidate already has the opposite owner decision")
        return candidate
    if approved:
        blocker = skill_replacement_approval_blocker(candidate)
        if blocker:
            raise SkillReplacementError(blocker)
    elif candidate.status != "test_passed":
        raise SkillReplacementError("only a test-passed skill candidate can be reviewed")
    record = {
        "candidate_fingerprint": candidate.fingerprint,
        "decision": wanted,
        "actor": _text(actor, "actor", 128),
        "note": _text(note, "note", 2000),
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _atomic_json(candidate.root / "review.json", record)
    manifest = dict(candidate.manifest)
    manifest.update({
        "status": record["decision"],
        "reviewed_at": record["reviewed_at"],
        "reviewed_by": record["actor"],
        "review_sha256": hashlib.sha256((candidate.root / "review.json").read_bytes()).hexdigest(),
    })
    _transition(
        manifest, action="reviewed", actor=record["actor"], before=candidate.status, after=record["decision"],
        reason="批准：测试通过，双跑评估通过；暂存并启用前不会安装" if approved else "拒绝：这个候选不会被暂存或启用",
    )
    _atomic_json(candidate.root / "candidate.json", manifest)
    return load_skill_replacement_candidate(candidate.root)


def revise_skill_replacement_candidate(
    candidate: SkillReplacementCandidate,
    current_skill_root: Path,
    payload: dict[str, Any],
    *,
    actor: str,
    reason: str,
    proposal_fingerprint: str = "",
) -> SkillReplacementCandidate:
    """Turn a failed candidate's fixed text into a new draft that names it (4.7-A03).

    The failed candidate keeps its status and every record; it only gains ``superseded_by``. The new one is an
    ordinary draft with its own fingerprint (so none of the old verdicts or evaluations can be carried over)
    and a ``revises`` link. It is made against the same live tree the failed one was; if the library moved
    since, the revision is refused -- edit the current version instead.
    """

    candidate = load_skill_replacement_candidate(candidate.root)
    actor = _text(actor, "actor", 128)
    reason = _text(reason, "reason", 500)
    evaluation_failed = candidate.status == "test_passed" and evaluation_verdict(candidate) == "failed"
    if candidate.status != "test_failed" and not evaluation_failed:
        raise SkillReplacementError("只有测试没通过、或双跑评估没通过的候选才能修订。")
    successor = _link_id(candidate.manifest.get("superseded_by"))
    if successor:
        raise SkillReplacementError("这个候选已经修订过一次：请在修订出的新候选上继续。")
    if skill_tree_fingerprint(current_skill_root) != str(candidate.manifest.get("current_fingerprint", "")):
        raise SkillReplacementError("技能在这个候选之后被改过：不能在它上面修订，基于现在的版本重新编辑。")
    revised = create_skill_replacement_candidate(
        candidate.root.parent, current_skill_root, payload, actor=actor,
        expected_current_fingerprint=str(candidate.manifest.get("current_fingerprint", "")),
        proposal_id=str(candidate.manifest.get("proposal_id", "")),
        proposal_fingerprint=proposal_fingerprint or str(candidate.manifest.get("proposal_fingerprint", "")),
    )
    if revised.candidate_id == candidate.candidate_id:
        raise SkillReplacementError("修订后的内容和原来完全一样：先改点什么再生成新候选。")
    parent = _link_id(revised.manifest.get("revises"))
    if parent and parent != candidate.candidate_id:
        raise SkillReplacementError("修订后的内容和另一个候选的修订一模一样：直接在那个候选上继续。")
    if not parent and revised.status != "draft":
        raise SkillReplacementError(f"修订后的内容和已有的候选 v{revised.manifest.get('version', '')} 一样，它已经走过测试：直接看那个候选。")
    why = "测试没通过" if candidate.status == "test_failed" else "双跑评估没通过"
    if not parent:
        manifest = dict(revised.manifest)
        manifest["revises"] = {"candidate_id": candidate.candidate_id, "candidate_fingerprint": candidate.fingerprint}
        _transition(
            manifest, action="revision_of", actor=actor, before="draft", after="draft",
            reason=f"由 v{candidate.manifest.get('version', '')} 的候选修订而来（那一版{why}）：{reason}",
        )
        _atomic_json(revised.root / "candidate.json", manifest)
    old = dict(candidate.manifest)
    old["superseded_by"] = {"candidate_id": revised.candidate_id, "candidate_fingerprint": revised.fingerprint}
    _transition(
        old, action="revised", actor=actor, before=candidate.status, after=candidate.status,
        reason=f"{why}，已修订成新候选 v{revised.manifest.get('version', '')}：{reason}",
    )
    _atomic_json(candidate.root / "candidate.json", old)
    return load_skill_replacement_candidate(revised.root)


def evaluation_verdict(candidate: SkillReplacementCandidate) -> str:
    """'passed', 'failed', or '' when no dual run is recorded."""

    if not candidate.manifest.get("evaluation_report_sha256"):
        return ""
    stored = str(candidate.manifest.get("evaluation_verdict", "") or "")
    if stored in {"passed", "failed"}:
        return stored
    report = _evaluation_report(candidate)
    evaluation = report.get("evaluation") if isinstance(report, dict) else None
    verdict = str(evaluation.get("verdict", "") or "") if isinstance(evaluation, dict) else ""
    return "passed" if verdict == "passed" else "failed"


def evaluation_standard(candidate: SkillReplacementCandidate) -> dict[str, Any] | None:
    """The frozen standard as the owner reads it (what is compared, by how much, how many samples), or None."""

    if not candidate.manifest.get("evaluation_spec_fingerprint"):
        return None
    try:
        spec = skill_replacement_evaluation_spec(candidate)
    except SkillReplacementError:
        return None
    return {
        "hypothesis": str(spec.payload.get("hypothesis", "")),
        "min_samples": int(spec.payload.get("min_samples", 0) or 0),
        "metrics": [
            {
                "name": item["name"], "label": _metric_word(item["name"]), "direction": item["direction"],
                "min_delta": item["min_delta"],
            }
            for item in spec.payload.get("metrics", [])
        ],
    }


def evaluation_summary(candidate: SkillReplacementCandidate) -> str:
    """The recorded dual run in one line the owner can read, or '' when there is none."""

    report = _evaluation_report(candidate) if candidate.manifest.get("evaluation_report_sha256") else None
    evaluation = report.get("evaluation") if isinstance(report, dict) else None
    return _evaluation_words(evaluation) if isinstance(evaluation, dict) else ""


def skill_replacement_approval_blocker(candidate: SkillReplacementCandidate) -> str:
    """Why this candidate cannot be approved now, in the owner's words; '' when it can (or is already decided).

    Approval needs, all at once: a passed test; a frozen spec whose asset is this exact candidate; a recorded
    comparison of that spec on this candidate's fingerprint; and a passed verdict.
    """

    status = candidate.status
    if status in {"approved", "rejected"}:
        return ""
    if status == "draft":
        return "还没有测试结论：先测试，测试通过后再做双跑评估，然后才能批准。"
    if status == "test_failed":
        return "测试没通过，不能批准：修订成新候选后再测。"
    if status != "test_passed":
        return "这个候选的状态不对，不能批准。"
    return skill_replacement_evaluation_blocker(candidate)


def skill_replacement_evaluation_blocker(candidate: SkillReplacementCandidate) -> str:
    """'' only when a passed dual run of this exact candidate is on record; else why not, in the owner's words.

    Approval checks it (through skill_replacement_approval_blocker) and staging checks it again, so a
    candidate approved before this gate existed cannot reach the library on someone else's evaluation.
    """

    if not candidate.manifest.get("evaluation_spec_fingerprint"):
        return "还没定双跑评估标准，不能批准：先定标准，再让新旧两版各跑一组，由系统比对。"
    try:
        spec = skill_replacement_evaluation_spec(candidate)
    except SkillReplacementError:
        return "双跑评估标准的记录读不出来或被改过，不能批准。"
    if spec.payload.get("asset") != skill_candidate_evaluation_asset(candidate):
        return "双跑评估测的不是这个候选（评估对象的指纹和当前候选对不上），不能批准。"
    if not candidate.manifest.get("evaluation_report_sha256"):
        return "双跑评估还没做，不能批准：新旧两版各跑一组、由系统比对通过后才能批准。"
    report = _evaluation_report(candidate)
    if not isinstance(report, dict):
        return "双跑评估的记录读不出来，不能批准。"
    evaluation = report.get("evaluation") if isinstance(report.get("evaluation"), dict) else {}
    if str(report.get("candidate_fingerprint", "") or "") != candidate.fingerprint:
        return "双跑评估记录的不是这个候选（指纹对不上），不能批准。"
    if (
        str(evaluation.get("spec_fingerprint", "") or "") != spec.fingerprint
        or str(evaluation.get("asset_fingerprint", "") or "") != spec.asset_fingerprint
    ):
        return "双跑评估用的标准和冻结的标准对不上，不能批准。"
    if str(evaluation.get("verdict", "") or "") != "passed":
        return f"{_evaluation_words(evaluation)}。不能批准：修订成新候选后再测。"
    return ""


def _evaluation_report(candidate: SkillReplacementCandidate) -> dict[str, Any] | None:
    try:
        report = json.loads((candidate.root / "evaluation-report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def _evaluation_words(evaluation: dict[str, Any]) -> str:
    """One comparison, in words: the verdict, then each measure (or only the failing ones when it failed)."""

    passed = str(evaluation.get("verdict", "") or "") == "passed"
    parts: list[str] = [
        _FLAG_WORDS.get(str(flag), "新旧两版的运行记录有重复") for flag in evaluation.get("reward_hack_flags", []) or []
    ]
    for measure in evaluation.get("metrics", []) or []:
        if not isinstance(measure, dict) or (not passed and measure.get("passed")):
            continue
        way = "提高" if measure.get("direction") == "higher" else "降低"
        parts.append(
            f"{_metric_word(str(measure.get('name', '')))}：旧版 {measure.get('baseline', '')}，新版 {measure.get('candidate', '')}，"
            f"要求{way}至少 {measure.get('required_improvement', '')}，实际{way} {measure.get('improvement', '')}"
        )
    head = "双跑评估通过" if passed else "双跑评估没通过"
    return head + ("：" + "；".join(parts) if parts else "")


def _metric_word(name: str) -> str:
    return _METRIC_WORDS.get(name, "一项自定义指标")


def _link_id(value: Any) -> str:
    return str(value.get("candidate_id", "") or "") if isinstance(value, dict) else ""


def _transition(manifest: dict[str, Any], *, action: str, actor: str, before: str, after: str, reason: str) -> None:
    """Append one audit entry (who, when, from -> to, why) to a manifest about to be written."""

    history = [dict(entry) for entry in manifest.get("transitions", []) or [] if isinstance(entry, dict)]
    history.append({
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actor": actor,
        "action": action,
        "from": before,
        "to": after,
        "reason": reason,
    })
    manifest["transitions"] = history


def _text(value: Any, name: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise SkillReplacementError(f"{name} is required and must be at most {limit} characters")
    return text


def _text_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 20:
        raise SkillReplacementError(f"{name} must contain 1-20 items")
    return [_text(item, f"{name} item", 300) for item in value]


def _sha(value: Any, name: str) -> str:
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", text):
        raise SkillReplacementError(f"{name} must be sha256")
    return text


def _no_secrets(values: list[str]) -> None:
    if any(pattern.search(value) for value in values for pattern in _SECRET_SHAPES):
        raise SkillReplacementError("skill replacement contains a secret-shaped value")


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
