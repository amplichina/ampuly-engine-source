"""Bounded, evidence-linked improvement proposals for supervised RSI.

This is deliberately not a source-code rewriter.  It accepts a narrow
declarative connector contract, binds it to immutable evidence references and
the fingerprint of the object being improved, and can materialize that payload
only through the existing connector-candidate lifecycle.  The resulting
candidate is still a draft: tests, owner review, registration and activation
remain separate gates.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .connector_candidate import ConnectorCandidate, create_candidate
from .connector_contract import from_payload as connector_from_payload
from .process_candidate import create_process_candidate
from .process_spec import (
    GATE_REQUIRED,
    ID as PROCESS_ID,
    STEP_KINDS,
    Assumption,
    ProcessSpec,
    ProcessSpecError,
    Step,
)
from .process_spec import from_payload as process_from_payload
from .rsi_evaluation import (
    FrozenEvaluationSpec,
    compare_evaluation_runs,
    freeze_evaluation_spec,
)
from .skill_factory import missing_skills
from .skill_replacement import (
    SkillReplacementCandidate,
    create_skill_replacement_candidate,
    normalize_skill_replacement,
)
from .workflow_packages import (
    INTAKE_FILLED_KEYS,
    WorkflowPackage,
    load_package,
    load_skill_contract,
    package_candidate_fingerprint,
)
from ampuly.core.config import replace_with_retry


# The product calls this target a process.  Proposal v1 persists the older
# wire token for backwards compatibility; consumers use this mapping instead
# of spreading that implementation identity through product-facing fixtures.
PROCESS_TARGET_KIND = "workflow"


class ImprovementProposalError(ValueError):
    """An improvement proposal that cannot be proven bounded and inert."""


@dataclass(frozen=True)
class ImprovementProposal:
    root: Path
    manifest: dict[str, Any]

    @property
    def proposal_id(self) -> str:
        return str(self.manifest.get("proposal_id", ""))

    @property
    def fingerprint(self) -> str:
        return str(self.manifest.get("proposal_fingerprint", ""))

    @property
    def status(self) -> str:
        return str(self.manifest.get("status", ""))


_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
_DRAFT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_PROPOSAL_ID = re.compile(r"^[a-z][a-z0-9_-]{1,191}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_TOP_KEYS = frozenset({"title", "rationale", "target", "evidence", "candidate"})
_TARGET_KEYS = frozenset({"kind", "id", "current_fingerprint"})
_EVIDENCE_KEYS = frozenset({"kind", "id", "sha256"})
_EVIDENCE_KINDS = frozenset({"acceptance", "incident", "probe", "run", "test"})
_SECRET_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9]{12,}"),
    re.compile(r"\bAKIA[A-Z0-9]{12,}"),
    re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}", re.IGNORECASE),
)


def project_improvement_root(ampuly_home: Path, project_id: str) -> Path:
    project = _required(project_id, "project_id")
    key = hashlib.sha256(project.encode("utf-8")).hexdigest()[:24]
    return Path(ampuly_home).resolve() / "improvement_proposals" / "projects" / key


def create_improvement_proposal(
    root: Path,
    payload: dict[str, Any],
    *,
    actor: str,
    project_id: str,
    conversation_id: str,
    turn_id: str,
) -> ImprovementProposal:
    """Persist one inert proposal after strict schema and contract validation."""

    actor = _required(actor, "actor")
    project_id = _required(project_id, "project_id")
    conversation_id = _required(conversation_id, "conversation_id")
    turn_id = _required(turn_id, "turn_id")
    normalized = _normalize(payload)
    canonical = _canonical(normalized)
    fingerprint = hashlib.sha256(canonical).hexdigest()
    target_id = str(normalized["target"]["id"])
    candidate_kind = str(normalized["candidate"]["kind"])
    prefix = {
        "connector_contract": "connector",
        "process_spec": "process",
        "skill_contract": "skill",
    }[candidate_kind]
    proposal_id = f"{prefix}-{_safe_slug(target_id)}-{fingerprint[:16]}"

    base = Path(root).resolve()
    base.mkdir(parents=True, exist_ok=True)
    proposal_root = (base / proposal_id).resolve()
    if proposal_root.parent != base:
        raise ImprovementProposalError("proposal path escaped its store")
    if proposal_root.exists():
        existing = load_improvement_proposal(proposal_root)
        if existing.fingerprint != fingerprint:
            raise ImprovementProposalError("proposal id collision")
        return existing

    now = _now()
    manifest = {
        "schema_version": 1,
        "proposal_id": proposal_id,
        "proposal_fingerprint": fingerprint,
        "status": "draft",
        "project_key": hashlib.sha256(project_id.encode("utf-8")).hexdigest(),
        "target": normalized["target"],
        "candidate_kind": candidate_kind,
        "created_at": now,
        "created_by": actor,
        "provenance": {
            "kind": "conversation",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "evidence_fingerprint": hashlib.sha256(
                _canonical(normalized["evidence"])
            ).hexdigest(),
        },
    }
    _write_new_bundle(proposal_root, {
        "proposal.json": manifest,
        "intent.json": {
            "title": normalized["title"],
            "rationale": normalized["rationale"],
            "target": normalized["target"],
        },
        "evidence.json": normalized["evidence"],
        "candidate-payload.json": normalized["candidate"],
    })
    return ImprovementProposal(proposal_root, manifest)


def materialize_connector_candidate(
    proposal: ImprovementProposal,
    candidate_root: Path,
    *,
    owner_actor: str,
    expected_fingerprint: str,
    observed_target_fingerprint: str,
) -> ConnectorCandidate:
    """Create an existing-governance draft; never test, approve or enable it."""

    owner_actor = _required(owner_actor, "owner_actor")
    _assert_current(proposal)
    if proposal.fingerprint != str(expected_fingerprint or "").strip():
        raise ImprovementProposalError("proposal fingerprint changed; reload before materializing")
    expected_target = str(proposal.manifest["target"]["current_fingerprint"])
    if expected_target != str(observed_target_fingerprint or "").strip().lower():
        raise ImprovementProposalError("target fingerprint changed; regenerate the improvement proposal")
    if proposal.status not in {"draft", "materialized"}:
        raise ImprovementProposalError("proposal is not materializable")
    if proposal.manifest.get("candidate_kind") != "connector_contract":
        raise ImprovementProposalError("proposal does not contain a connector contract")
    candidate_payload = _read_json(proposal.root / "candidate-payload.json")
    connector = create_candidate(
        Path(candidate_root),
        {
            "summary": _read_json(proposal.root / "intent.json")["rationale"],
            "contract": candidate_payload["payload"],
            "improvement_provenance": {
                "proposal_id": proposal.proposal_id,
                "proposal_fingerprint": proposal.fingerprint,
            },
        },
        actor=owner_actor,
        conversation_id=str(proposal.manifest["provenance"]["conversation_id"]),
        turn_id=str(proposal.manifest["provenance"]["turn_id"]),
    )
    if proposal.status == "draft":
        manifest = dict(proposal.manifest)
        manifest.update({
            "status": "materialized",
            "materialized_at": _now(),
            "materialized_by": owner_actor,
            "candidate_id": connector.candidate_id,
            "candidate_fingerprint": connector.fingerprint,
            "candidate_status": connector.status,
        })
        _atomic_json(proposal.root / "proposal.json", manifest)
    return connector


def materialize_process_candidate(
    proposal: ImprovementProposal,
    template: WorkflowPackage,
    *,
    ampuly_home: Path,
    draft_id: str,
    owner_actor: str,
    expected_fingerprint: str,
    allow_draft_missing_skills: bool = False,
) -> WorkflowPackage:
    """Compile a ProcessSpec through the existing draft-only package writer.

    A matching draft left behind after a crash is recovered; an unrelated
    pre-existing directory is never overwritten or adopted.
    """

    owner_actor = _required(owner_actor, "owner_actor")
    _assert_current(proposal)
    if proposal.fingerprint != str(expected_fingerprint or "").strip():
        raise ImprovementProposalError("proposal fingerprint changed; reload before materializing")
    if proposal.status not in {"draft", "materialized"}:
        raise ImprovementProposalError("proposal is not materializable")
    if proposal.manifest.get("candidate_kind") != "process_spec":
        raise ImprovementProposalError("proposal does not contain a process spec")
    target = dict(proposal.manifest.get("target", {}) or {})
    if str(template.id) != str(target.get("id", "")):
        raise ImprovementProposalError("workflow template does not match proposal target")
    if package_candidate_fingerprint(template) != str(target.get("current_fingerprint", "")):
        raise ImprovementProposalError("target fingerprint changed; regenerate the improvement proposal")

    candidate_payload = _read_json(proposal.root / "candidate-payload.json")
    spec = process_from_payload(candidate_payload["payload"])
    missing = missing_skills(spec, template)
    if missing and not allow_draft_missing_skills:
        raise ImprovementProposalError(
            "process proposal needs explicit owner permission to draft missing skills: " + ", ".join(missing)
        )
    clean_draft_id = str(draft_id or "").strip().lower()
    if not _DRAFT_ID.fullmatch(clean_draft_id):
        raise ImprovementProposalError("draft_id must be lowercase letters, digits, '-' or '_' (2-64 chars)")
    destination = Path(ampuly_home).resolve() / "workflows" / clean_draft_id
    if destination.exists() and _linked(destination):
        raise ImprovementProposalError("linked process candidate directories are not supported")
    existing = load_package(destination) if destination.exists() else None
    if existing is not None:
        provenance = dict(existing.manifest.get("provenance", {}) or {})
        generated = sorted(
            str(item.get("id", ""))
            for item in list(provenance.get("generated_skills", []) or [])
            if isinstance(item, dict) and str(item.get("id", ""))
        )
        if (
            existing.status != "draft"
            or provenance.get("spec_digest") != spec.digest()
            or provenance.get("template_id") != template.id
            or provenance.get("template_version") != str(template.manifest.get("version", ""))
            or generated != sorted(missing)
        ):
            raise ImprovementProposalError("draft id belongs to another process candidate")
        process = existing
    else:
        process = create_process_candidate(
            spec, template, ampuly_home=Path(ampuly_home), draft_id=clean_draft_id,
            actor=owner_actor, generate_missing_skills=allow_draft_missing_skills,
        )
    if proposal.status == "draft":
        manifest = dict(proposal.manifest)
        manifest.update({
            "status": "materialized",
            "materialized_at": _now(),
            "materialized_by": owner_actor,
            "candidate_id": process.id,
            "candidate_fingerprint": package_candidate_fingerprint(process),
            "candidate_status": process.status,
            "draft_missing_skills_authorized": bool(allow_draft_missing_skills),
            "generated_skill_ids": list(missing),
        })
        _atomic_json(proposal.root / "proposal.json", manifest)
    return process


def materialize_skill_candidate(
    proposal: ImprovementProposal,
    current_skill_root: Path,
    candidate_root: Path,
    *,
    owner_actor: str,
    expected_fingerprint: str,
) -> SkillReplacementCandidate:
    """Create an inert versioned Skill candidate; never alter the internal library."""

    owner_actor = _required(owner_actor, "owner_actor")
    _assert_current(proposal)
    if proposal.fingerprint != str(expected_fingerprint or "").strip():
        raise ImprovementProposalError("proposal fingerprint changed; reload before materializing")
    if proposal.status not in {"draft", "materialized"}:
        raise ImprovementProposalError("proposal is not materializable")
    if proposal.manifest.get("candidate_kind") != "skill_contract":
        raise ImprovementProposalError("proposal does not contain a skill contract")
    target = dict(proposal.manifest.get("target", {}) or {})
    if Path(current_skill_root).name != str(target.get("id", "")):
        raise ImprovementProposalError("current skill does not match proposal target")
    candidate_payload = _read_json(proposal.root / "candidate-payload.json")
    candidate = create_skill_replacement_candidate(
        Path(candidate_root),
        Path(current_skill_root),
        candidate_payload["payload"],
        actor=owner_actor,
        expected_current_fingerprint=str(target.get("current_fingerprint", "")),
        proposal_id=proposal.proposal_id,
        proposal_fingerprint=proposal.fingerprint,
    )
    if proposal.status == "draft":
        manifest = dict(proposal.manifest)
        manifest.update({
            "status": "materialized",
            "materialized_at": _now(),
            "materialized_by": owner_actor,
            "candidate_id": candidate.candidate_id,
            "candidate_fingerprint": candidate.fingerprint,
            "candidate_status": candidate.status,
        })
        _atomic_json(proposal.root / "proposal.json", manifest)
    return candidate


def improvement_asset_identity(proposal: ImprovementProposal) -> dict[str, str]:
    """Exact generic RSI identity of a materialized candidate.

    The proposal is the common parent of process, connector and Skill
    candidates.  Keeping their evaluation binding here avoids teaching the
    numeric comparator three unrelated lifecycle formats, while the actual
    candidates remain inert and governed by their own release gates.
    """
    proposal = load_improvement_proposal(proposal.root)
    if proposal.status != "materialized":
        raise ImprovementProposalError("only a materialized proposal has a candidate evaluation identity")
    kind = str(proposal.manifest.get("candidate_kind", "") or "")
    asset_kind = {
        "process_spec": "process",
        "connector_contract": "connector",
        "skill_contract": "skill",
    }.get(kind)
    if not asset_kind:
        raise ImprovementProposalError("proposal candidate kind has no evaluation identity")
    candidate = _read_json(proposal.root / "candidate-payload.json")
    payload = dict(candidate.get("payload", {}) or {})
    version = _bounded_text(payload.get("version"), "candidate version", 64)
    fingerprint = str(proposal.manifest.get("candidate_fingerprint", "") or "").strip().lower()
    if not _SHA256.fullmatch(fingerprint):
        raise ImprovementProposalError("materialized candidate fingerprint must be sha256")
    return {
        "asset_kind": asset_kind,
        "asset_id": str(dict(proposal.manifest.get("target", {}) or {}).get("id", "")),
        "version": version,
        "candidate_fingerprint": fingerprint,
        "manifest_sha256": hashlib.sha256((proposal.root / "candidate-payload.json").read_bytes()).hexdigest(),
    }


def bind_improvement_evaluation(
    proposal: ImprovementProposal,
    spec: FrozenEvaluationSpec,
) -> ImprovementProposal:
    """Freeze one generic comparison contract beside an inert candidate."""
    proposal = load_improvement_proposal(proposal.root)
    expected = improvement_asset_identity(proposal)
    if spec.payload.get("asset") != expected:
        raise ImprovementProposalError("evaluation spec asset differs from the materialized candidate")
    path = proposal.root / "evaluation-spec.json"
    existing_fingerprint = str(proposal.manifest.get("evaluation_spec_fingerprint", "") or "")
    if path.exists():
        if existing_fingerprint != spec.fingerprint:
            raise ImprovementProposalError("proposal already uses another evaluation spec")
        return load_improvement_proposal(proposal.root)
    _atomic_json(path, spec.to_payload())
    manifest = dict(proposal.manifest)
    manifest.update({
        "evaluation_spec_fingerprint": spec.fingerprint,
        "evaluation_spec_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    })
    _atomic_json(proposal.root / "proposal.json", manifest)
    return load_improvement_proposal(proposal.root)


def improvement_evaluation_spec(proposal: ImprovementProposal) -> FrozenEvaluationSpec:
    """Load and verify the frozen evaluation contract bound to this proposal."""

    proposal = load_improvement_proposal(proposal.root)
    path = proposal.root / "evaluation-spec.json"
    if not path.is_file():
        raise ImprovementProposalError("proposal has no frozen evaluation spec")
    stored = _read_json(path)
    supplied_fingerprint = str(stored.pop("spec_fingerprint", "") or "")
    if stored.pop("schema_version", None) != 1:
        raise ImprovementProposalError("proposal evaluation spec version is unsupported")
    try:
        spec = freeze_evaluation_spec(stored)
    except ValueError as exc:
        raise ImprovementProposalError(str(exc)) from exc
    if (
        spec.fingerprint != supplied_fingerprint
        or spec.fingerprint != str(proposal.manifest.get("evaluation_spec_fingerprint", "") or "")
        or spec.payload.get("asset") != improvement_asset_identity(proposal)
    ):
        raise ImprovementProposalError("proposal evaluation spec identity changed")
    return spec


def record_improvement_evaluation(
    proposal: ImprovementProposal,
    *,
    baseline: dict[str, Any],
    candidate_run: dict[str, Any],
    actor: str,
    note: str,
) -> ImprovementProposal:
    """Attach a machine-verifiable dual run without approving or activating."""
    proposal = load_improvement_proposal(proposal.root)
    if proposal.status != "materialized":
        raise ImprovementProposalError("only a materialized proposal can receive evaluation evidence")
    try:
        spec = improvement_evaluation_spec(proposal)
        report = compare_evaluation_runs(spec, baseline, candidate_run)
    except ValueError as exc:
        raise ImprovementProposalError(str(exc)) from exc
    record = {
        "proposal_fingerprint": proposal.fingerprint,
        "candidate_fingerprint": str(proposal.manifest.get("candidate_fingerprint", "")),
        "evaluation": report,
        "actor": _required(actor, "actor"),
        "note": _bounded_text(note, "note", 2000),
        "recorded_at": _now(),
    }
    report_path = proposal.root / "evaluation-report.json"
    _atomic_json(report_path, record)
    manifest = dict(proposal.manifest)
    manifest.update({
        "evaluation_verdict": report["verdict"],
        "evaluation_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "evaluated_at": record["recorded_at"],
        "evaluated_by": record["actor"],
    })
    _atomic_json(proposal.root / "proposal.json", manifest)
    return load_improvement_proposal(proposal.root)


def load_improvement_proposal(root: Path) -> ImprovementProposal:
    root = Path(root).resolve()
    manifest = _read_json(root / "proposal.json")
    proposal = ImprovementProposal(root, manifest)
    _assert_current(proposal)
    return proposal


def improvement_proposal_by_id(root: Path, proposal_id: str) -> ImprovementProposal:
    proposal_id = str(proposal_id or "").strip()
    if not _PROPOSAL_ID.fullmatch(proposal_id):
        raise ImprovementProposalError("proposal id is invalid")
    base = Path(root).resolve()
    target = (base / proposal_id).resolve()
    if target.parent != base or not target.is_dir() or target.is_symlink():
        raise ImprovementProposalError("proposal does not exist")
    return load_improvement_proposal(target)


def list_improvement_proposals(root: Path) -> list[dict[str, Any]]:
    """Return bounded, non-secret proposal metadata for one project store."""

    base = Path(root).resolve()
    if not base.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(base.iterdir(), key=lambda item: item.name)[:500]:
        if not path.is_dir() or _linked(path):
            continue
        try:
            proposal = load_improvement_proposal(path)
            intent = _read_json(path / "intent.json")
        except ImprovementProposalError:
            continue
        rows.append({
            "proposal_id": proposal.proposal_id,
            "proposal_fingerprint": proposal.fingerprint,
            "status": proposal.status,
            "title": str(intent.get("title", "")),
            "target": dict(proposal.manifest.get("target", {}) or {}),
            "candidate_kind": str(proposal.manifest.get("candidate_kind", "")),
            "created_at": str(proposal.manifest.get("created_at", "")),
            "created_by": str(proposal.manifest.get("created_by", "")),
            "evaluation_spec_fingerprint": str(
                proposal.manifest.get("evaluation_spec_fingerprint", "")
            ),
            "evaluation_verdict": str(proposal.manifest.get("evaluation_verdict", "")),
            # 4.7-A11: where the candidate is (to open its editor), what real orders said, and the decision's reason.
            "candidate_id": str(proposal.manifest.get("candidate_id", "") or ""),
            "candidate_status": str(proposal.manifest.get("candidate_status", "") or ""),
            "benefit": _benefit_row(proposal.manifest.get("benefit")),
            "decision_reason": str(proposal.manifest.get("decision_reason", "") or ""),
            "reviewed_at": str(proposal.manifest.get("reviewed_at", "") or ""),
        })
    return rows


def reject_improvement_proposal(
    proposal: ImprovementProposal,
    *,
    owner_actor: str,
    expected_fingerprint: str,
    note: str,
) -> ImprovementProposal:
    """Record an owner rejection without deleting evidence or creating a candidate."""

    owner_actor = _required(owner_actor, "owner_actor")
    note = _bounded_text(note, "note", 1000)
    _assert_current(proposal)
    if proposal.fingerprint != str(expected_fingerprint or "").strip():
        raise ImprovementProposalError("proposal fingerprint changed; reload before rejecting")
    if proposal.status == "rejected":
        return proposal
    if proposal.status != "draft":
        raise ImprovementProposalError("only a draft improvement proposal can be rejected")
    review = {
        "proposal_fingerprint": proposal.fingerprint,
        "decision": "rejected",
        "actor": owner_actor,
        "note": note,
        "reviewed_at": _now(),
    }
    _atomic_json(proposal.root / "review.json", review)
    manifest = dict(proposal.manifest)
    manifest.update({
        "status": "rejected",
        "reviewed_at": review["reviewed_at"],
        "review_sha256": hashlib.sha256((proposal.root / "review.json").read_bytes()).hexdigest(),
    })
    _atomic_json(proposal.root / "proposal.json", manifest)
    return ImprovementProposal(proposal.root, manifest)


# ── 4.7-A11: an improvement asked for from a verified asset's detail, judged on real orders ──
#
# The owner says one sentence on a published flow, an internal-library skill or a registered connector. The server
# drafts the whole governed proposal from the asset as it is now (its fingerprint, its verified release, the recent
# orders it ran) and materializes it through the existing inert candidate writers above; nothing is tested, approved,
# staged or published here. Whether the change paid off is not a number anybody types: it is the last N finished
# real orders before and after, compared on how long they took, how often they were sent back and what the model
# calls cost. A key measure that got worse beyond the threshold refuses the approval, in words, naming the measure;
# too few finished orders is said as it is and approves nothing.

IMPROVEMENT_TARGET_KINDS = {"process": PROCESS_TARGET_KIND, "skill": "skill", "connector": "connector"}
_CANDIDATE_KIND_OF = {"process": "process_spec", "skill": "skill_contract", "connector": "connector_contract"}
REQUEST_LIMIT = 200
DEFAULT_BENEFIT_SAMPLE = 5
MAX_BENEFIT_SAMPLE = 50
DEFAULT_WORSE_THRESHOLD = 0.10
# (measure, what the owner reads, unit). Every one is "lower is better".
BENEFIT_METRICS = (
    ("duration_hours", "完成时长", "小时"),
    ("rework_count", "打回次数", "次"),
    ("model_cost", "模型花费", "美元"),
)
_BENEFIT_VERDICTS = frozenset({"insufficient", "negative", "neutral", "positive"})
# Node keys a ProcessSpec step carries; anything else a template step holds is named back to the owner.
_STEP_NODE_KEYS = frozenset({
    "id", "name", "type", "role", "assignee", "inputs", "deliverables", "writes", "tools", "skills", "depends_on",
    "responsibility", "standard", "gate", "gate_to", "branch_when", "side_effect", "invoke", "time_limit_hours",
    "model", "canvas",
})
_SETTING_WORDS = {
    "connector": "网站连接器设置", "rework_to": "打回去向", "revision_path": "修改单位置", "tier": "模型档位",
    "components": "组件参数", "skill_versions": "技能版本锁定",
}


def clean_improvement_request(value: Any) -> str:
    """The owner's one sentence, or a refusal that says what is wrong with it."""
    text = " ".join(str(value or "").split())
    if not text:
        raise ImprovementProposalError("写一句话说清楚要改进什么。")
    if len(text) > REQUEST_LIMIT:
        raise ImprovementProposalError(f"一句话说清楚就行（{REQUEST_LIMIT} 字以内）。")
    if any(pattern.search(text) for pattern in _SECRET_SHAPES):
        raise ImprovementProposalError("这句话里有像密钥的字符串：删掉再提。")
    return text


def next_minor_version(version: Any) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(version or "").strip())
    if not match:
        raise ImprovementProposalError("现在的版本号不是 1.2.3 这样的三段数字，没法排出改进后的版本号。")
    return f"{match.group(1)}.{int(match.group(2)) + 1}.0"


def draft_process_payload(package: WorkflowPackage, request: str, *, author: str) -> tuple[dict[str, Any], list[str]]:
    """A complete ProcessSpec of a published flow as it is now, carrying the owner's request as a stated assumption.

    Returns the payload and, in the owner's words, the step settings a process description cannot carry (they are
    not copied into the draft and have to be set again on the builder). A gap the flow itself has -- no acceptance
    criteria, a step with no standard -- is a refusal naming it, never a filled-in default.
    """
    request = clean_improvement_request(request)
    manifest = dict(package.manifest or {})
    workflow_id = str(package.id or "")
    if not PROCESS_ID.fullmatch(workflow_id):
        raise ImprovementProposalError("这条流程的编号里有流程描述不支持的字符，不能从一句话生成改进草稿：在搭建台复制一份草稿再改。")
    version = next_minor_version(manifest.get("version"))
    assumptions = [Assumption(about="这次要改进什么", took=request, because="老板提出；草稿里还没改，要在搭建台落实")]
    names: dict[str, str] = {}
    steps: list[Step] = []
    not_carried: list[str] = []
    for node in list(package.nodes or []):
        if not isinstance(node, dict):
            raise ImprovementProposalError("这条流程有一步读不出来，不能生成改进草稿。")
        node_id = str(node.get("id", "") or "")
        name = str(node.get("name") or node_id)
        kind = str(node.get("type", "") or "ai_do")
        if kind not in STEP_KINDS or kind == "external_call":
            raise ImprovementProposalError(f"「{name}」这一步的类型流程描述不支持，不能从一句话生成改进草稿。")
        extra = sorted(key for key in node if key not in _STEP_NODE_KEYS)
        if extra:
            words = list(dict.fromkeys(_SETTING_WORDS.get(key, "其他设置") for key in extra))
            not_carried.append(f"「{name}」的{'、'.join(words)}")
        human_nodes = kind in {"human_do", "human_review"}
        try:
            steps.append(Step(
                id=node_id, name=name, kind=kind, role=str(node.get("role", "") or ""),
                inputs=_texts(node.get("inputs")), outputs=_texts(node.get("deliverables")),
                writes=_texts(node.get("writes")), tools=_texts(node.get("tools")), skills=_texts(node.get("skills")),
                depends_on=_texts(node.get("depends_on")),
                responsibility=str(node.get("responsibility") or name), standard=_texts(node.get("standard")),
                human_gate=str(node.get("gate", "") or "") == GATE_REQUIRED,
                gate_to=str(node.get("gate_to") or (node.get("assignee") if human_nodes else "") or "owner"),
                branch_when=str(node.get("branch_when", "") or ""), side_effect=str(node.get("side_effect", "") or ""),
                invoke=dict(node.get("invoke") or {}) if isinstance(node.get("invoke"), dict) else {},
                time_limit_hours=_whole(node.get("time_limit_hours")), model=str(node.get("model", "") or ""),
            ))
        except ProcessSpecError as exc:
            raise ImprovementProposalError(f"「{name}」这一步写法流程描述读不了，不能从一句话生成改进草稿（{exc}）。") from exc
        names[node_id] = name
    acceptance = manifest.get("acceptance")
    if isinstance(acceptance, dict):
        acceptance = acceptance.get("criteria")
    criteria = _texts(acceptance)
    if not criteria:
        raise ImprovementProposalError("这条流程没有写验收标准，先在搭建台补上再提改进。")
    constraints = dict(manifest.get("process_constraints", {}) or {})
    trigger = str(manifest.get("summary", "") or "").strip()
    if not trigger:
        trigger = f"老板为「{package.name}」下单"
        assumptions.append(Assumption(about="什么时候开始", took=trigger, because="流程里没写，沿用下单即开始"))
    finishes_when = str(constraints.get("finishes_when", "") or "").strip()
    if not finishes_when:
        finishes_when = "客户验收通过"
        assumptions.append(Assumption(about="到什么程度算结束", took=finishes_when, because="流程里没写，沿用验收即结束"))
    data_scope = str(constraints.get("data_scope", "") or "").strip()
    if not data_scope:
        data_scope = "只处理本单的资料"
        assumptions.append(Assumption(about="能碰哪些数据", took=data_scope, because="流程里没写，沿用只碰本单"))
    fields = copy.deepcopy(list(package.intake_fields or []))
    for item in fields:
        if isinstance(item, dict):
            for key in INTAKE_FILLED_KEYS:
                item.pop(key, None)
    try:
        spec = ProcessSpec(
            spec_id=workflow_id, name=f"{package.name}（改进）"[:160], version=version, trigger=trigger,
            finishes_when=finishes_when, intake_fields=[dict(item) for item in fields if isinstance(item, dict)],
            steps=steps, acceptance_criteria=criteria, data_scope=data_scope,
            time_limit_hours=_whole(constraints.get("time_limit_hours")), assumptions=assumptions,
            author=_required(author, "author"),
        )
    except ProcessSpecError as exc:
        raise ImprovementProposalError(f"这条流程的写法流程描述读不了，不能从一句话生成改进草稿（{exc}）。") from exc
    gaps = spec.missing()
    if gaps:
        readable = []
        for gap in gaps:
            head, _, rest = gap.partition(": ")
            readable.append(f"「{names[head]}」{rest}" if rest and head in names else gap)
        raise ImprovementProposalError("这条流程还缺这些，先在搭建台补上再提改进：" + "；".join(readable) + "。")
    return spec.to_payload(), not_carried


def draft_skill_payload(skill_root: Path, request: str) -> dict[str, Any]:
    """A replacement of one library skill: its own text plus the owner's request, at the next minor version."""
    request = clean_improvement_request(request)
    root = Path(skill_root)
    contract = load_skill_contract(root.parent, root.name)
    if not contract.complete:
        raise ImprovementProposalError("这个技能的发布契约不完整，先在「契约」里补齐再提改进：" + "；".join(contract.problems or ["缺版本、来源、测试或成功标准"]))
    try:
        text = (root / "SKILL.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise ImprovementProposalError("这个技能的说明读不出来。") from exc
    meta, body = _front_matter_split(text)
    description = str(meta.get("description", "") or "").strip() or str(meta.get("name", "") or root.name)
    instructions = f"{body.strip()}\n\n## 改进要求\n\n- {request}\n".strip() + "\n"
    if len(instructions) > 20000:
        raise ImprovementProposalError("这个技能的说明太长，不能从一句话生成候选：在「契约」里直接编辑。")
    criteria = [item for item in contract.success_criteria if len(item) <= 300][:19] + [f"做到：{request}"]
    return {
        "skill_id": root.name,
        "version": next_minor_version(contract.version),
        "description": description[:300],
        "instructions": instructions,
        "success_criteria": list(dict.fromkeys(criteria)),
        "test_plan": [f"找一单照这条改进要求试一次，看是否做到：{request}"],
    }


def draft_connector_payload(contract_payload: dict[str, Any], request: str) -> dict[str, Any]:
    """The registered contract at the next minor version; the request travels as the candidate's summary."""
    clean_improvement_request(request)
    payload = copy.deepcopy(dict(contract_payload or {}))
    payload["version"] = next_minor_version(payload.get("version"))
    return payload


def improvement_request_payload(
    target_kind: str,
    target_id: str,
    current_fingerprint: str,
    request: str,
    candidate_payload: dict[str, Any],
    *,
    asset_version: str,
    order_evidence: Iterable[dict[str, str]] = (),
) -> dict[str, Any]:
    """The whole governed proposal, bound to the asset as verified now and to the recent orders it ran."""
    kind = str(target_kind or "").strip()
    if kind not in IMPROVEMENT_TARGET_KINDS:
        raise ImprovementProposalError("改进对象只能是流程、技能或连接器。")
    request = clean_improvement_request(request)
    fingerprint = str(current_fingerprint or "").strip().lower()
    if not _SHA256.fullmatch(fingerprint):
        raise ImprovementProposalError("这个对象现在没有可核对的指纹，不能提改进。")
    verified_id = f"verified:{kind}:{target_id}:{asset_version or 'current'}"
    if not _ID.fullmatch(verified_id):
        verified_id = f"verified:{kind}:{fingerprint[:16]}"
    evidence = [{"kind": "test", "id": verified_id, "sha256": fingerprint}, *list(order_evidence)][:50]
    return {
        "title": request if len(request) <= 160 else request[:159] + "…",
        "rationale": request,
        "target": {"kind": IMPROVEMENT_TARGET_KINDS[kind], "id": str(target_id), "current_fingerprint": fingerprint},
        "evidence": evidence,
        "candidate": {"kind": _CANDIDATE_KIND_OF[kind], "payload": candidate_payload},
    }


def order_run_evidence(business: Any, orders: Iterable[Any], *, limit: int = DEFAULT_BENEFIT_SAMPLE) -> list[dict[str, str]]:
    """The newest orders with a verified event chain, each bound by its last event's hash."""
    rows: list[dict[str, str]] = []
    for order in orders:
        if len(rows) >= max(0, int(limit)):
            break
        order_id = str(getattr(order, "order_id", "") or "")
        if not _ID.fullmatch(order_id):
            continue
        try:
            if not business.verify_order_event_chain(order_id):
                continue
            events = list(business.events(order_id) or [])
        except Exception:  # noqa: BLE001 - an unreadable order is simply not evidence
            continue
        digest = str(dict((events[-1] if events else {}).get("integrity", {}) or {}).get("event_hash", "") or "").lower()
        if _SHA256.fullmatch(digest):
            rows.append({"kind": "run", "id": order_id, "sha256": digest})
    return rows


@dataclass(frozen=True)
class OrderBenefitFact:
    """What one real order took: how long, how often it was sent back, what its model calls cost (None = unknown)."""

    order_id: str
    workflow_id: str
    created_at: str
    finished_at: str
    duration_hours: float | None
    rework_count: int
    model_cost: float | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id, "workflow_id": self.workflow_id, "created_at": self.created_at,
            "finished_at": self.finished_at, "duration_hours": self.duration_hours,
            "rework_count": self.rework_count, "model_cost": self.model_cost,
        }


def order_benefit_fact(business: Any, order: Any, *, spend: Mapping[str, float] | None = None) -> OrderBenefitFact:
    """Read one order's measures from the business store's own records.

    Finished means the customer accepted it (``acceptance.accepted``) or it was closed after a delivery. Sent back
    counts the customer's rework rounds and every gate the reviewer answered 打回. The model spend is the usage
    ledger's total for the order's tasks; an order the ledger does not know has an unknown spend, not a zero one.
    """
    order_id = str(getattr(order, "order_id", "") or "")
    events = list(business.events(order_id) or [])
    finished_at = next((str(e.get("at", "") or "") for e in events if e.get("kind") == "acceptance.accepted"), "")
    if not finished_at and str(getattr(order, "status", "") or "") == "closed" and any(e.get("kind") == "delivering" for e in events):
        finished_at = next((str(e.get("at", "") or "") for e in reversed(events) if e.get("kind") == "closed"), "")
    rework = sum(1 for e in events if e.get("kind") == "acceptance.rework_started")
    cards = getattr(business, "gate_cards_for_order", None)
    if callable(cards):
        rework += sum(1 for card in list(cards(order_id) or []) if str(dict(card).get("decision", "") or "") == "reject")
    created_at = str(getattr(order, "created_at", "") or "")
    start, end = _instant(created_at), _instant(finished_at)
    duration = round((end - start).total_seconds() / 3600, 4) if start and end and end >= start else None
    cost = None
    if spend is not None and order_id in spend:
        try:
            cost = round(float(spend[order_id]), 6)
        except (TypeError, ValueError):
            cost = None
    return OrderBenefitFact(
        order_id=order_id, workflow_id=str(getattr(order, "workflow_id", "") or ""), created_at=created_at,
        finished_at=finished_at if duration is not None else "", duration_hours=duration, rework_count=rework,
        model_cost=cost,
    )


def spend_by_order(cost_by_task: Mapping[str, Mapping[str, Any]], tasks: Iterable[Any]) -> dict[str, float]:
    """Model spend per order from the usage ledger (cost rows filed against tasks that name their order).

    An order with a task on record and no cost row spent nothing that was recorded (0.0); an order with no task on
    record is absent, which the benefit check reads as unknown.
    """
    totals: dict[str, float] = {}
    for task in tasks:
        metadata = dict(getattr(task, "metadata", {}) or {})
        order_id = str(metadata.get("order_id", "") or "").strip()
        if not order_id:
            continue
        spent = dict(cost_by_task.get(str(getattr(task, "id", "") or "")) or {})
        try:
            totals[order_id] = totals.get(order_id, 0.0) + float(spent.get("cost", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
    return totals


def select_benefit_samples(
    facts: Iterable[OrderBenefitFact],
    *,
    baseline_workflows: Iterable[str],
    candidate_workflows: Iterable[str],
    switch_at: str = "",
    until: str = "",
    sample_size: int = DEFAULT_BENEFIT_SAMPLE,
) -> tuple[list[OrderBenefitFact], list[OrderBenefitFact]]:
    """The newest N finished orders before and after: on the old and new flow, or either side of the switch."""
    size = _sample_size(sample_size)
    base_ids, trial_ids = set(baseline_workflows), set(candidate_workflows)
    switch, end = _instant(switch_at), _instant(until)
    finished = [fact for fact in facts if fact.finished_at and _instant(fact.created_at) is not None]
    baseline = [
        fact for fact in finished
        if fact.workflow_id in base_ids and (switch is None or _instant(fact.created_at) < switch)
    ]
    candidate = [
        fact for fact in finished
        if fact.workflow_id in trial_ids
        and (switch is None or _instant(fact.created_at) >= switch)
        and (end is None or _instant(fact.created_at) < end)
    ]
    newest = lambda rows: sorted(rows, key=lambda fact: _instant(fact.finished_at) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:size]  # noqa: E731
    return newest(baseline), newest(candidate)


def judge_order_benefit(
    baseline: list[OrderBenefitFact],
    candidate: list[OrderBenefitFact],
    *,
    sample_size: int = DEFAULT_BENEFIT_SAMPLE,
    threshold: float = DEFAULT_WORSE_THRESHOLD,
    basis: str = "",
) -> dict[str, Any]:
    """Compare the two samples; any key measure worse beyond the threshold is a negative benefit."""
    size = _sample_size(sample_size)
    limit = _threshold(threshold)
    report: dict[str, Any] = {
        "schema_version": 1, "sample_size": size, "threshold": limit, "basis": str(basis or ""),
        "baseline": {"count": len(baseline), "order_ids": [fact.order_id for fact in baseline]},
        "candidate": {"count": len(candidate), "order_ids": [fact.order_id for fact in candidate]},
        "metrics": [], "worsened": [], "improved": [],
        "evidence_sha256": hashlib.sha256(_canonical({
            "baseline": [fact.to_payload() for fact in baseline],
            "candidate": [fact.to_payload() for fact in candidate],
        })).hexdigest(),
    }
    if len(baseline) < size or len(candidate) < size:
        report["verdict"] = "insufficient"
        report["reason"] = (
            f"样本不够，不能判定收益：旧版本、新版本各要至少 {size} 单做完的真实订单，"
            f"现在旧版本 {len(baseline)} 单、新版本 {len(candidate)} 单。" + (f"{basis}" if basis else "")
        )
        return report
    for name, label, unit in BENEFIT_METRICS:
        before_values = [getattr(fact, name) for fact in baseline]
        after_values = [getattr(fact, name) for fact in candidate]
        if any(value is None for value in [*before_values, *after_values]):
            report["metrics"].append({"name": name, "label": label, "unit": unit, "judged": False,
                                      "note": f"{label}有订单读不到，没有参与判断"})
            continue
        before = sum(float(value) for value in before_values) / len(before_values)
        after = sum(float(value) for value in after_values) / len(after_values)
        change = after - before
        ratio = None if before == 0 else change / before
        worse = change > 0 and (ratio is None or ratio > limit)
        better = change < 0 and ratio is not None and -ratio > limit
        row = {
            "name": name, "label": label, "unit": unit, "judged": True,
            "baseline": round(before, 4), "candidate": round(after, 4),
            "change_ratio": None if ratio is None else round(ratio, 4), "worse": worse, "better": better,
        }
        report["metrics"].append(row)
        if worse:
            report["worsened"].append(label)
        if better:
            report["improved"].append(label)
    rows = {row["name"]: row for row in report["metrics"] if row.get("judged")}
    if report["worsened"]:
        report["verdict"] = "negative"
        report["reason"] = "负收益，不能批准：" + "；".join(
            _benefit_change_words(row) for row in report["metrics"] if row.get("worse")
        ) + f"（变差超过 {limit * 100:.0f}% 就算负收益）。"
    elif report["improved"]:
        report["verdict"] = "positive"
        report["reason"] = "有收益：" + "；".join(
            _benefit_change_words(row) for row in report["metrics"] if row.get("better")
        ) + "；没有关键指标变差。"
    else:
        report["verdict"] = "neutral"
        report["reason"] = f"关键指标没有变差超过 {limit * 100:.0f}%，也没有明显变好。"
    if not rows:
        report["verdict"] = "insufficient"
        report["reason"] = "样本不够，不能判定收益：这些订单的指标都读不到。"
    return report


# LP-B P-g review fix (2026-09-27): two refusals, each saying a remedy that works. A candidate changed since the real
# orders were judged clears on re-evaluation (the comparison is then kept under the candidate as it is now); a skill or
# connector candidate is never changed once made (a revision is another candidate), so one no longer as made, or any
# candidate that cannot be read, does not.
CANDIDATE_CHANGED = "候选在评估之后被改过，请重新评估"
CANDIDATE_NOT_AS_PROPOSED = "候选在生成改进提案之后被改过或读不出来，不能批准"


def record_improvement_benefit(
    proposal: ImprovementProposal,
    report: dict[str, Any],
    *,
    actor: str,
    candidate_fingerprint: str | None = None,
) -> ImprovementProposal:
    """Keep the latest real-order comparison on the proposal itself (one atomic manifest write).

    ``candidate_fingerprint`` (LP-B P-g, 2026-09-27): the candidate's fingerprint as it is now, read by the caller from
    the candidate itself. The comparison is kept under that value, so a later decision can tell whether the candidate
    it judged is still the one on disk; each evaluation re-binds it, the value written at materialize is not touched.
    Left out, the value written at materialize is kept, as before.
    """
    actor = _required(actor, "actor")
    proposal = load_improvement_proposal(proposal.root)
    if proposal.status != "materialized":
        raise ImprovementProposalError("只有已生成候选、还没有决定的改进才能评估收益。")
    if not isinstance(report, dict) or report.get("verdict") not in _BENEFIT_VERDICTS:
        raise ImprovementProposalError("收益评估结果不完整。")
    evaluated = (
        str(proposal.manifest.get("candidate_fingerprint", "") or "")
        if candidate_fingerprint is None else str(candidate_fingerprint or "").strip().lower()
    )
    manifest = dict(proposal.manifest)
    manifest.update({
        "benefit": {
            **report,
            "candidate_fingerprint": evaluated,
            "evaluated_at": _now(),
            "evaluated_by": actor,
        },
        "benefit_verdict": str(report["verdict"]),
    })
    _atomic_json(proposal.root / "proposal.json", manifest)
    return load_improvement_proposal(proposal.root)


def decide_improvement_proposal(
    proposal: ImprovementProposal,
    *,
    owner_actor: str,
    expected_fingerprint: str,
    note: str = "",
    live_fingerprint: str | None = None,
) -> tuple[ImprovementProposal, bool, str]:
    """Approve on a recorded real-order comparison, or refuse: negative benefit rejects, too few orders decides nothing.

    Approval is the improvement's own decision; the candidate still goes through its release chain (a flow's release
    approval and publication, a skill's staging and activation, a connector's registration), unchanged.

    ``live_fingerprint`` (LP-B P-g, 2026-09-27): the candidate's fingerprint read from the candidate itself right now.
    Until now the check compared the value written at materialize with a copy of itself, so a candidate edited after
    the real orders were judged passed. Given, it must be readable and equal the fingerprint the comparison was kept
    under (``CANDIDATE_CHANGED``; re-evaluating clears it). A skill or connector candidate must also still be the one
    the proposal was made with (``CANDIDATE_NOT_AS_PROPOSED``). A flow draft is not held to that value: it is made as
    a copy of the published flow that the owner finishes on the builder (the request travels only as an assumption),
    and it takes real orders only once submitted for testing, which changes its status and test records, so by the
    time there is a benefit to approve it has always moved. Otherwise nothing is decided and the proposal stays open.
    """
    owner_actor = _required(owner_actor, "owner_actor")
    proposal = load_improvement_proposal(proposal.root)
    if proposal.fingerprint != str(expected_fingerprint or "").strip():
        raise ImprovementProposalError("proposal fingerprint changed; reload before deciding")
    if proposal.status == "approved":
        return proposal, True, str(proposal.manifest.get("decision_reason", "") or "已经批准过。")
    if proposal.status != "materialized":
        raise ImprovementProposalError("只有已生成候选、还没有决定的改进才能批准。")
    benefit = dict(proposal.manifest.get("benefit", {}) or {})
    if not benefit:
        raise ImprovementProposalError("还没有按真实订单评估收益，不能批准。")
    made_with = str(proposal.manifest.get("candidate_fingerprint", "") or "")
    evaluated = str(benefit.get("candidate_fingerprint", "") or "")
    if live_fingerprint is None:
        # The caller did not read the candidate: as before, the comparison must be kept under the proposal's own value.
        if evaluated != made_with:
            raise ImprovementProposalError("候选在评估之后变过，重新评估收益再批准。")
    else:
        live = str(live_fingerprint or "").strip().lower()
        fixed_once_made = str(proposal.manifest.get("candidate_kind", "") or "") != "process_spec"
        if not live or (fixed_once_made and live != made_with):
            raise ImprovementProposalError(CANDIDATE_NOT_AS_PROPOSED)
        if live != evaluated:
            raise ImprovementProposalError(CANDIDATE_CHANGED)
    verdict = str(benefit.get("verdict", "") or "")
    reason = str(benefit.get("reason", "") or "")
    if verdict == "insufficient":
        raise ImprovementProposalError(reason or "样本不够，不能判定收益。")
    if verdict not in {"negative", "neutral", "positive"}:
        raise ImprovementProposalError("收益评估结果不完整，重新评估再批准。")
    approved = verdict != "negative"
    note = str(note or "").strip()
    review = {
        "proposal_fingerprint": proposal.fingerprint,
        "decision": "approved" if approved else "rejected",
        "basis": "real_order_benefit",
        "reason": reason,
        "worsened": list(benefit.get("worsened", []) or []),
        "benefit_evidence_sha256": str(benefit.get("evidence_sha256", "") or ""),
        "actor": owner_actor,
        "note": _bounded_text(note, "note", 1000) if note else "",
        "reviewed_at": _now(),
    }
    _atomic_json(proposal.root / "review.json", review)
    manifest = dict(proposal.manifest)
    manifest.update({
        "status": "approved" if approved else "rejected",
        "reviewed_at": review["reviewed_at"],
        "reviewed_by": owner_actor,
        "decision_reason": reason,
        "review_sha256": hashlib.sha256((proposal.root / "review.json").read_bytes()).hexdigest(),
    })
    _atomic_json(proposal.root / "proposal.json", manifest)
    return load_improvement_proposal(proposal.root), approved, reason


def _benefit_change_words(row: dict[str, Any]) -> str:
    unit = str(row.get("unit", "") or "")
    digits = 4 if row.get("name") == "model_cost" else 1 if row.get("name") == "duration_hours" else 2
    before, after = float(row.get("baseline", 0) or 0), float(row.get("candidate", 0) or 0)
    ratio = row.get("change_ratio")
    how = "从 0 变多" if ratio is None else f"{'多' if after > before else '少'}了 {abs(float(ratio)) * 100:.0f}%"
    return f"{row.get('label')}平均每单从 {before:.{digits}f} {unit}变成 {after:.{digits}f} {unit}（{how}）"


def _benefit_row(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value:
        return None
    return {
        key: value.get(key)
        for key in ("verdict", "reason", "sample_size", "threshold", "basis", "metrics", "worsened", "improved",
                    "evaluated_at")
    } | {
        "baseline_count": int(dict(value.get("baseline", {}) or {}).get("count", 0) or 0),
        "candidate_count": int(dict(value.get("candidate", {}) or {}).get("count", 0) or 0),
    }


def _sample_size(value: Any) -> int:
    if isinstance(value, bool):
        raise ImprovementProposalError("样本数要是整数。")
    try:
        size = int(value)
    except (TypeError, ValueError) as exc:
        raise ImprovementProposalError("样本数要是整数。") from exc
    if not 1 <= size <= MAX_BENEFIT_SAMPLE:
        raise ImprovementProposalError(f"样本数要在 1 到 {MAX_BENEFIT_SAMPLE} 单之间。")
    return size


def _threshold(value: Any) -> float:
    try:
        limit = float(value)
    except (TypeError, ValueError) as exc:
        raise ImprovementProposalError("变差阈值要是 0 到 1 之间的比例。") from exc
    if not 0 <= limit < 1:
        raise ImprovementProposalError("变差阈值要是 0 到 1 之间的比例。")
    return limit


def _instant(text: Any) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.astimezone()


def _texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _whole(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _front_matter_split(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    try:
        loaded = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        loaded = {}
    return (loaded if isinstance(loaded, dict) else {}), text[end + 4:]


def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ImprovementProposalError("proposal must be an object")
    _exact_keys(payload, _TOP_KEYS, "proposal")
    title = _bounded_text(payload.get("title"), "title", 160)
    rationale = _bounded_text(payload.get("rationale"), "rationale", 2000)

    target = payload.get("target")
    if not isinstance(target, dict):
        raise ImprovementProposalError("target must be an object")
    _exact_keys(target, _TARGET_KEYS, "target")
    target_kind = str(target.get("kind", "") or "")
    if target_kind not in {"connector", PROCESS_TARGET_KIND, "skill"}:
        raise ImprovementProposalError("target.kind must be connector, workflow or skill")
    target_id = _bounded_id(target.get("id"), "target.id")
    current = str(target.get("current_fingerprint", "") or "").strip().lower()
    if not _SHA256.fullmatch(current):
        raise ImprovementProposalError("target.current_fingerprint must be sha256")

    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence or len(evidence) > 50:
        raise ImprovementProposalError("evidence must contain 1-50 references")
    normalized_evidence: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(evidence):
        if not isinstance(raw, dict):
            raise ImprovementProposalError(f"evidence[{index}] must be an object")
        _exact_keys(raw, _EVIDENCE_KEYS, f"evidence[{index}]")
        kind = str(raw.get("kind", "") or "").strip()
        if kind not in _EVIDENCE_KINDS:
            raise ImprovementProposalError(f"evidence[{index}].kind is not supported")
        evidence_id = _bounded_id(raw.get("id"), f"evidence[{index}].id")
        digest = str(raw.get("sha256", "") or "").strip().lower()
        if not _SHA256.fullmatch(digest):
            raise ImprovementProposalError(f"evidence[{index}].sha256 must be sha256")
        row = (kind, evidence_id, digest)
        if row not in seen:
            seen.add(row)
            normalized_evidence.append({"kind": kind, "id": evidence_id, "sha256": digest})

    candidate = payload.get("candidate")
    if not isinstance(candidate, dict) or set(candidate) != {"kind", "payload"}:
        raise ImprovementProposalError("candidate must contain only kind and payload")
    candidate_kind = str(candidate.get("kind", "") or "")
    if candidate_kind == "connector_contract":
        if target_kind != "connector":
            raise ImprovementProposalError("connector contracts require a connector target")
        normalized_payload = connector_from_payload(candidate.get("payload")).to_payload()
        if normalized_payload["name"] != target_id:
            raise ImprovementProposalError("connector contract name must match target.id")
    elif candidate_kind == "process_spec":
        if target_kind != PROCESS_TARGET_KIND:
            raise ImprovementProposalError("process specs require a workflow target")
        spec = process_from_payload(candidate.get("payload"))
        if spec.spec_id != target_id:
            raise ImprovementProposalError("process spec id must match target.id")
        if not spec.ready:
            raise ImprovementProposalError("process spec is incomplete: " + "; ".join(spec.missing()))
        normalized_payload = spec.to_payload()
    elif candidate_kind == "skill_contract":
        if target_kind != "skill":
            raise ImprovementProposalError("skill contracts require a skill target")
        normalized_payload = normalize_skill_replacement(candidate.get("payload"))
        if normalized_payload["skill_id"] != target_id:
            raise ImprovementProposalError("skill contract id must match target.id")
    else:
        raise ImprovementProposalError("candidate.kind must be connector_contract, process_spec or skill_contract")

    return {
        "title": title,
        "rationale": rationale,
        "target": {"kind": target_kind, "id": target_id, "current_fingerprint": current},
        "evidence": normalized_evidence,
        "candidate": {"kind": candidate_kind, "payload": normalized_payload},
    }


def _assert_current(proposal: ImprovementProposal) -> None:
    if proposal.root.is_symlink() or not proposal.root.is_dir():
        raise ImprovementProposalError("proposal root is not a real directory")
    manifest = _read_json(proposal.root / "proposal.json")
    if manifest != proposal.manifest:
        raise ImprovementProposalError("proposal manifest changed; reload")
    intent = _read_json(proposal.root / "intent.json")
    evidence = _read_json(proposal.root / "evidence.json")
    candidate = _read_json(proposal.root / "candidate-payload.json")
    normalized = _normalize({
        "title": intent.get("title"),
        "rationale": intent.get("rationale"),
        "target": intent.get("target"),
        "evidence": evidence,
        "candidate": candidate,
    })
    actual = hashlib.sha256(_canonical(normalized)).hexdigest()
    if actual != proposal.fingerprint:
        raise ImprovementProposalError("proposal content changed after creation")
    for field, relative in (
        ("evaluation_spec_sha256", "evaluation-spec.json"),
        ("evaluation_report_sha256", "evaluation-report.json"),
    ):
        digest = str(proposal.manifest.get(field, "") or "")
        path = proposal.root / relative
        if digest:
            if (
                not _SHA256.fullmatch(digest)
                or not path.is_file()
                or _linked(path)
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest
            ):
                raise ImprovementProposalError(f"proposal evaluation evidence changed: {relative}")
        elif path.exists():
            raise ImprovementProposalError(f"proposal has unbound evaluation evidence: {relative}")


def _write_new_bundle(root: Path, files: dict[str, Any]) -> None:
    parent = root.parent
    temp = Path(tempfile.mkdtemp(prefix=f".{root.name}.", dir=parent))
    try:
        for name, payload in files.items():
            _atomic_json(temp / name, payload)
        replace_with_retry(temp, root)
    except Exception:
        for path in temp.glob("*"):
            path.unlink(missing_ok=True)
        temp.rmdir()
        raise


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path) -> Any:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ImprovementProposalError(f"invalid proposal file: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImprovementProposalError(f"invalid proposal file: {path.name}") from exc


def _exact_keys(value: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    extra = set(value) - allowed
    missing = allowed - set(value)
    if extra or missing:
        raise ImprovementProposalError(
            f"{where} fields differ: missing={sorted(missing)} extra={sorted(extra)}"
        )


def _required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ImprovementProposalError(f"{name} is required")
    return text


def _bounded_text(value: Any, name: str, limit: int) -> str:
    text = _required(value, name)
    if len(text) > limit or "\x00" in text:
        raise ImprovementProposalError(f"{name} is too long or invalid")
    if any(pattern.search(text) for pattern in _SECRET_SHAPES):
        raise ImprovementProposalError(f"{name} looks like a secret value")
    return text


def _bounded_id(value: Any, name: str) -> str:
    text = _required(value, name)
    if not _ID.fullmatch(text) or any(pattern.search(text) for pattern in _SECRET_SHAPES):
        raise ImprovementProposalError(f"{name} is invalid")
    return text


def _safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower()


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
