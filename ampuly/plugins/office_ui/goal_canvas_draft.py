"""Q5: turn a goal (+ materials digest) into an original canvas draft.

Engineering chain only. The default planner is a fixture heuristic — no paid
model, no network. It writes through the real package APIs
(``create_blank_workflow_draft`` + ``update_package_nodes``) so the draft loads
like any owner-authored blank flow. Model-quality evaluation stays a separate
ledger line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from ampuly.project_id import is_valid_project_id
from ampuly.plugins.office_ui.workflow_packages import (
    create_blank_workflow_draft,
    update_package_nodes,
)

_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


class GoalCanvasDraftRefused(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "refused")
        self.detail = str(detail or "")
        super().__init__(f"{self.code}: {self.detail}" if self.detail else self.code)


@dataclass
class GoalDraftPlan:
    name: str
    summary: str
    intake_fields: list[dict[str, Any]]
    nodes: list[dict[str, Any]]
    planner: str = "fixture_heuristic"
    paid_api: bool = False


@dataclass
class GoalCanvasDraftResult:
    draft_id: str
    name: str
    version: str
    draft_revision: str
    node_ids: list[str]
    planner: str
    paid_api: bool
    created_from: str
    problems: list[Any] = field(default_factory=list)
    root: str = ""
    project_id: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "name": self.name,
            "version": self.version,
            "draft_revision": self.draft_revision,
            "node_ids": list(self.node_ids),
            "planner": self.planner,
            "paid_api": self.paid_api,
            "created_from": self.created_from,
            "problems": list(self.problems),
            "root": self.root,
            "project_id": self.project_id,
            "canvas_draft": True,
        }


Planner = Callable[[str, str, str], GoalDraftPlan]


def _slug_draft_id(raw: str, *, fallback: str = "goal_draft") -> str:
    text = str(raw or "").strip().lower().replace(" ", "_")
    text = _SLUG_RE.sub("-", text).strip("-_")
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"g_{text}"
    return text[:64]


def _first_role_id(org_source: Path) -> str:
    try:
        org = yaml.safe_load(Path(org_source).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise GoalCanvasDraftRefused("org_unreadable", str(exc)) from exc
    roles = list(org.get("roles") or [])
    if not roles:
        raise GoalCanvasDraftRefused("org_has_no_roles")
    role_id = str((roles[0] or {}).get("id") or "").strip()
    if not role_id:
        raise GoalCanvasDraftRefused("org_role_missing_id")
    return role_id


def fixture_heuristic_plan(goal: str, materials_digest: str = "", role_id: str = "worker") -> GoalDraftPlan:
    """Deterministic, offline plan: research → draft deliverable. Never paid."""
    clean = " ".join(str(goal or "").split())
    if not clean:
        raise GoalCanvasDraftRefused("goal_empty")
    preview = clean if len(clean) <= 48 else clean[:47] + "…"
    digest = str(materials_digest or "").strip()
    summary = f"Q5 fixture draft from goal; materials_digest={digest[:16] or 'none'}"
    nodes = [
        {
            "id": "research_goal",
            "name": "调研目标与材料",
            "type": "ai_do",
            "role": role_id,
            "responsibility": f"根据目标整理事实与约束：\n{str(goal or '')}",
            "deliverables": ["notes/goal_research.md"],
        },
        {
            "id": "draft_deliverable",
            "name": "起草交付提纲",
            "type": "ai_do",
            "role": role_id,
            "responsibility": "产出可审阅的交付提纲（非最终客户件）",
            "deliverables": ["drafts/outline.md"],
        },
    ]
    return GoalDraftPlan(
        name=f"目标：{preview}",
        summary=summary,
        intake_fields=[
            {"id": "goal", "label": "目标", "required": True},
            {"id": "materials", "label": "材料摘要", "required": False},
        ],
        nodes=nodes,
        planner="fixture_heuristic",
        paid_api=False,
    )


def generate_canvas_draft_from_goal(
    *,
    ampuly_home: Path | str,
    org_source: Path | str,
    goal: str,
    actor: str = "owner",
    draft_id: str = "",
    materials_digest: str = "",
    message_id: str = "",
    planner: Planner | None = None,
    allow_paid_planner: bool = False,
    project_id: str = "",
) -> GoalCanvasDraftResult:
    """Create a real draft package from a goal. Default planner uses no paid API.

    A paid (real-model) plan is refused unless the caller opts in with
    ``allow_paid_planner=True`` (see ``real_model_evidence``).
    """
    if not str(actor or "").strip():
        raise GoalCanvasDraftRefused("actor_missing")
    role_id = _first_role_id(Path(org_source))
    if planner is None:
        plan = fixture_heuristic_plan(goal, materials_digest, role_id=role_id)
    else:
        plan = planner(str(goal or ""), str(materials_digest or ""), role_id)
        if plan.paid_api and not allow_paid_planner:
            raise GoalCanvasDraftRefused("paid_api_forbidden_in_q5_leaf")

    mid = str(message_id or "").strip()
    created_from = f"q5_goal:{mid}" if mid else "q5_goal"
    clean_id = _slug_draft_id(draft_id or (f"q5_{mid}" if mid else "q5_goal_draft"))

    package = create_blank_workflow_draft(
        ampuly_home=Path(ampuly_home),
        draft_id=clean_id,
        actor=str(actor).strip(),
        org_source=Path(org_source),
        name=plan.name,
        summary=plan.summary,
        intake_fields=list(plan.intake_fields),
        created_from=created_from,
        project_id=project_id,
    )
    rev = package.to_payload()["draft_revision"]
    saved = update_package_nodes(
        package,
        list(plan.nodes),
        actor=str(actor).strip(),
        expected_version=str(package.manifest.get("version") or "0.1.0"),
        new_version="0.1.1",
        expected_revision=rev,
        change_note=f"q5 goal→canvas draft ({plan.planner})",
    )
    payload = saved.to_payload()
    return GoalCanvasDraftResult(
        draft_id=saved.id,
        name=str(saved.manifest.get("name") or plan.name),
        version=str(saved.manifest.get("version") or ""),
        draft_revision=str(payload.get("draft_revision") or ""),
        node_ids=[str(n.get("id") or "") for n in list(saved.nodes or [])],
        planner=plan.planner,
        paid_api=bool(plan.paid_api),
        created_from=created_from,
        problems=list(saved.problems or []),
        root=str(saved.root),
        project_id=str(saved.manifest.get("project_id") or ""),
    )


def draft_from_admission(
    admission: Any,
    *,
    ampuly_home: Path | str,
    org_source: Path | str,
    actor: str,
    draft_id: str = "",
    planner: Planner | None = None,
) -> GoalCanvasDraftResult:
    """Preserve admitted input in a draft; this grants no execution authority."""
    status = str(getattr(admission, "status", "") or "")
    accepted = bool(getattr(admission, "accepted", False))
    if not accepted or status != "skeleton_accepted":
        raise GoalCanvasDraftRefused("admission_not_skeleton_accepted", status or "not_accepted")
    # A legacy preview cannot establish the original goal; never reconstruct it.
    goal = str(getattr(admission, "goal", "") or "")
    if not goal.strip():
        raise GoalCanvasDraftRefused("admission_goal_empty")
    project_id = getattr(admission, "project_id", "")
    if not is_valid_project_id(project_id):
        raise GoalCanvasDraftRefused("admission_project_invalid")
    return generate_canvas_draft_from_goal(
        ampuly_home=ampuly_home,
        org_source=org_source,
        goal=goal,
        actor=actor,
        draft_id=draft_id,
        materials_digest=str(getattr(admission, "materials_digest", "") or ""),
        message_id=str(getattr(admission, "message_id", "") or ""),
        planner=planner,
        project_id=project_id,
    )
