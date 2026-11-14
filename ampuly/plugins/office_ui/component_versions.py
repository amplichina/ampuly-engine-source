"""组件 · 编辑 / 测试 / 发布 for one internal-library skill (4.7 A2-4, docs/4.7-A2-CAPABILITY-CARDS.zh.md).

Nothing new is invented here. An owner's edit becomes the same inert replacement candidate an improvement proposal
makes (skill_replacement): a draft beside the library with its diff, never a change to the live skill. Its test
verdict and the owner's review are that module's own records; staging, activation and rollback stay with
skill_activation, which still demands a frozen dual-run evaluation before anything reaches the library.

The candidate names where it came from (proposal "owner-edit" plus a hash of the edit), so an owner's edit and an
improvement proposal can be told apart in every record, and the same edit asked twice is the same candidate.

4.7-A03 walks the whole chain from here: test -> (failed: revise into a new draft) -> freeze the dual-run standard ->
the dual run itself (ConsoleAPI, which holds the run records) -> approval, which now needs that passed run of this
exact candidate -> staging, activation, rollback (skill_activation, unchanged).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ampuly.plugins.office_ui.rsi_evaluation import freeze_evaluation_spec
from ampuly.plugins.office_ui.skill_activation import list_skill_activations, project_skill_activation_root
from ampuly.plugins.office_ui.skill_replacement import (
    SkillReplacementError,
    bind_skill_replacement_evaluation,
    create_skill_replacement_candidate,
    list_skill_replacement_candidates,
    project_skill_candidate_root,
    record_skill_replacement_test,
    review_skill_replacement_candidate,
    revise_skill_replacement_candidate,
    skill_candidate_evaluation_asset,
    skill_replacement_by_id,
    skill_tree_fingerprint,
)

OWNER_EDIT = "owner-edit"
MAX_DIFF_CHARS = 20000
MIN_NOTE_CHARS = 8
_FIELDS = ("version", "description", "instructions", "success_criteria", "test_plan")
# 4.7-A03: the measures a dual run can take from the run records themselves (harness_evaluation), each with the
# direction that counts as better. Nothing else is offered: a number a person types is not evidence.
DUAL_RUN_METRICS = {
    "completion_rate": "higher",
    "artifact_rate": "higher",
    "failure_rate": "lower",
    "human_pause_rate": "lower",
}
MAX_SAMPLES = 1000


class ComponentVersionError(ValueError):
    """An edit, a verdict or a review the chain refuses; the message says why."""


def _skill_root(skills_root: Path, skill_id: str) -> Path:
    from ampuly.plugins.office_ui.workflow_packages import LIBRARY_SKILL_ID

    clean = str(skill_id or "").strip()
    if not LIBRARY_SKILL_ID.match(clean):
        raise ComponentVersionError(f"{clean!r} is not a library skill id")
    root = Path(skills_root).resolve()
    folder = (root / clean).resolve()
    if folder.parent != root or not (folder / "SKILL.md").is_file():
        raise ComponentVersionError(f"the internal library holds no skill {clean!r}")
    return folder


def _diff(candidate_root: Path) -> tuple[str, bool]:
    try:
        text = (candidate_root / "DIFF.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", False
    return text[:MAX_DIFF_CHARS], len(text) > MAX_DIFF_CHARS


def _test_note(candidate_root: Path) -> str:
    try:
        record = json.loads((candidate_root / "test-result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(record.get("note", "") or "") if isinstance(record, dict) else ""


def _edit_fields(candidate_root: Path) -> dict[str, Any] | None:
    """The candidate's own text, so a revision starts from what failed rather than from the live skill."""
    try:
        payload = json.loads((candidate_root / "payload.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {key: payload.get(key) for key in _FIELDS} if isinstance(payload, dict) else None


def skill_versions(ampuly_home: Path, project_id: str, skills_root: Path, skill_id: str) -> dict[str, Any]:
    """The live skill's fingerprint (what an edit is made against) and every candidate of it, newest first."""
    folder = _skill_root(skills_root, skill_id)
    fingerprint = skill_tree_fingerprint(folder)
    deployments = {
        str(item.manifest.get("candidate_id", "")): item.to_payload()
        for item in list_skill_activations(project_skill_activation_root(ampuly_home, project_id))
    }
    rows: list[dict[str, Any]] = []
    for candidate in list_skill_replacement_candidates(project_skill_candidate_root(ampuly_home, project_id)):
        if str(candidate.manifest.get("skill_id", "")) != folder.name:
            continue
        diff, truncated = _diff(candidate.root)
        rows.append({
            **candidate.to_payload(),
            "source": "owner_edit" if str(candidate.manifest.get("proposal_id", "")) == OWNER_EDIT else "improvement_proposal",
            # Made against the live tree as it is now, or against one that has changed since (then it can no
            # longer be staged: the activation checks the same fingerprint).
            "current": str(candidate.manifest.get("current_fingerprint", "")) == fingerprint,
            "test_note": _test_note(candidate.root),
            "diff": diff,
            "diff_truncated": truncated,
            "activation": deployments.get(candidate.candidate_id),
            "edit": _edit_fields(candidate.root),
        })
    return {"skill_id": folder.name, "fingerprint": fingerprint, "candidates": rows}


def create_edit(
    ampuly_home: Path, project_id: str, skills_root: Path, skill_id: str, fields: dict[str, Any], *,
    actor: str, expected_fingerprint: str,
) -> dict[str, Any]:
    """An owner's edit as an inert draft candidate. The live skill is not touched."""
    folder = _skill_root(skills_root, skill_id)
    if not isinstance(fields, dict) or set(fields) - set(_FIELDS):
        raise ComponentVersionError("an edit carries version, description, instructions, success_criteria and test_plan only")
    payload = {"skill_id": folder.name, **{key: fields.get(key) for key in _FIELDS}}
    identity = _edit_identity(payload)
    try:
        candidate = create_skill_replacement_candidate(
            project_skill_candidate_root(ampuly_home, project_id), folder, payload,
            actor=actor, expected_current_fingerprint=expected_fingerprint,
            proposal_id=OWNER_EDIT, proposal_fingerprint=identity,
        )
    except SkillReplacementError as exc:
        raise ComponentVersionError(str(exc)) from exc
    diff, truncated = _diff(candidate.root)
    return {**candidate.to_payload(), "source": "owner_edit", "current": True, "diff": diff, "diff_truncated": truncated}


def record_test(ampuly_home: Path, project_id: str, candidate_id: str, *, outcome: str, note: str, actor: str) -> dict[str, Any]:
    """The owner's verdict on a draft candidate, with what was checked. Nothing is run by this call."""
    if len(str(note or "").strip()) < MIN_NOTE_CHARS:
        raise ComponentVersionError(f"say what was checked (at least {MIN_NOTE_CHARS} characters)")
    try:
        candidate = skill_replacement_by_id(project_skill_candidate_root(ampuly_home, project_id), candidate_id)
        return record_skill_replacement_test(candidate, outcome=outcome, actor=actor, note=str(note).strip()).to_payload()
    except SkillReplacementError as exc:
        raise ComponentVersionError(str(exc)) from exc


def review(
    ampuly_home: Path, project_id: str, candidate_id: str, *, approved: bool, note: str, actor: str, expected_fingerprint: str,
) -> dict[str, Any]:
    """Approve or reject a test-passed candidate. Approval installs nothing: staging and activation stay separate."""
    try:
        candidate = skill_replacement_by_id(project_skill_candidate_root(ampuly_home, project_id), candidate_id)
        return review_skill_replacement_candidate(
            candidate, approved=approved, actor=actor, note=str(note or "").strip() or ("approved" if approved else "rejected"),
            expected_fingerprint=expected_fingerprint,
        ).to_payload()
    except SkillReplacementError as exc:
        raise ComponentVersionError(str(exc)) from exc


def revise(
    ampuly_home: Path, project_id: str, skills_root: Path, candidate_id: str, fields: dict[str, Any], *,
    actor: str, reason: str, expected_fingerprint: str,
) -> dict[str, Any]:
    """A failed candidate's fix as a new draft that names it (4.7-A03). The failed one and the live skill stay as they are."""
    if len(str(reason or "").strip()) < MIN_NOTE_CHARS:
        raise ComponentVersionError(f"写明这次改了什么（至少 {MIN_NOTE_CHARS} 个字）。")
    if not isinstance(fields, dict) or set(fields) - set(_FIELDS):
        raise ComponentVersionError("an edit carries version, description, instructions, success_criteria and test_plan only")
    try:
        candidate = skill_replacement_by_id(project_skill_candidate_root(ampuly_home, project_id), candidate_id)
        if candidate.fingerprint != str(expected_fingerprint or "").strip():
            raise ComponentVersionError("这个候选在这之后变过：刷新后再修订。")
        folder = _skill_root(skills_root, str(candidate.manifest.get("skill_id", "")))
        payload = {"skill_id": folder.name, **{key: fields.get(key) for key in _FIELDS}}
        # An owner's edit keeps the owner-edit identity (a hash of the text); a proposal's candidate keeps its proposal.
        owner_edit = str(candidate.manifest.get("proposal_id", "")) == OWNER_EDIT
        revised = revise_skill_replacement_candidate(
            candidate, folder, payload, actor=actor, reason=str(reason).strip(),
            proposal_fingerprint=_edit_identity(payload) if owner_edit else "",
        )
    except SkillReplacementError as exc:
        raise ComponentVersionError(str(exc)) from exc
    diff, truncated = _diff(revised.root)
    return {**revised.to_payload(), "source": "owner_edit" if owner_edit else "improvement_proposal", "current": True,
            "diff": diff, "diff_truncated": truncated}


def bind_evaluation(
    ampuly_home: Path, project_id: str, candidate_id: str, *,
    hypothesis: str, metrics: list[dict[str, Any]], min_samples: Any, actor: str, expected_fingerprint: str,
) -> dict[str, Any]:
    """Freeze how this candidate's dual run is judged, before any sample of it runs (4.7-A03).

    The owner says what should get better and by how much; what is measured -- this candidate's exact SKILL.md --
    is filled in here from the candidate itself, never typed. Once bound it cannot be changed.
    """
    text = str(hypothesis or "").strip()
    if not text or len(text) > 1000:
        raise ComponentVersionError("写一句这一版应该比旧版好在哪（1000 字以内）。")
    if not isinstance(metrics, list) or not 1 <= len(metrics) <= len(DUAL_RUN_METRICS):
        raise ComponentVersionError("至少选一项要比较的指标。")
    rows: list[dict[str, Any]] = []
    for item in metrics:
        name = str((item or {}).get("name", "") if isinstance(item, dict) else "").strip()
        if name not in DUAL_RUN_METRICS or any(row["name"] == name for row in rows):
            raise ComponentVersionError("只能比较系统能从运行记录里算出来的指标：完成率、交出产物的比例、失败率、停下来等人的比例，每项一次。")
        try:
            delta = float((item or {}).get("min_delta"))
        except (TypeError, ValueError):
            delta = 0.0
        if not 0 < delta <= 1:
            raise ComponentVersionError("每项指标要求的提升要大于 0、不超过 1（比例，例如 0.1 就是 10 个百分点）。")
        rows.append({"name": name, "direction": DUAL_RUN_METRICS[name], "min_delta": delta, "lower_bound": 0, "upper_bound": 1})
    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or not 1 <= min_samples <= MAX_SAMPLES:
        raise ComponentVersionError(f"每组样本数要是 1 到 {MAX_SAMPLES} 之间的整数。")
    try:
        candidate = skill_replacement_by_id(project_skill_candidate_root(ampuly_home, project_id), candidate_id)
        if candidate.fingerprint != str(expected_fingerprint or "").strip():
            raise ComponentVersionError("这个候选在这之后变过：刷新后再定评估标准。")
        spec = freeze_evaluation_spec({
            "asset": skill_candidate_evaluation_asset(candidate), "hypothesis": text,
            "metrics": rows, "min_samples": min_samples,
        })
        return bind_skill_replacement_evaluation(candidate, spec, actor=actor).to_payload()
    except (SkillReplacementError, ValueError) as exc:
        if isinstance(exc, ComponentVersionError):
            raise
        raise ComponentVersionError(str(exc)) from exc


def _edit_identity(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
