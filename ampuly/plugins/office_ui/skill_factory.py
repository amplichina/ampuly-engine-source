"""Draft Skill contracts from a description, and say they are drafts.

A step that names a skill the template does not have used to be a dead end:
"template is missing skills". That is the right answer when nobody asked for
a skill to be made. It is the wrong one when the description is the only
thing that exists yet, because the next move was somebody hand-writing a
SKILL.md from the same description.

This writes that file. The skill the step describes becomes a SKILL.md with
the release contract the package loader reads -- version, provenance,
tests, success criteria -- inside the candidate's own assets, so a candidate
can be loaded and tried with it. And it says, in every field that can carry
it, that nobody has reviewed or run it: version 0.1.0, ``status: draft``,
provenance ``source: process_spec`` with ``reviewed_at`` empty, and a
``tests/contract.md`` whose outcome is ``unrun``. The loader turns
``status: draft`` into a contract problem, so a drafted skill cannot be
locked for production until a person reviews it and records that they did.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import yaml

from ampuly.plugins.office_ui.process_spec import ProcessSpec, Step
from ampuly.plugins.office_ui.workflow_packages import (
    _guard_manifest_writer,
    WorkflowPackage,
    _parse_front_matter,
    _write_manifest_atomic,
    validate_package,
)

SKILL_ID = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
DRAFT_VERSION = "0.1.0"
# What a person has to add before a drafted skill is more than words.
STILL_NEEDED: tuple[str, ...] = (
    "方法与参考：这一步具体怎么做、参照什么，描述里没有",
    "人工评审：谁看过、什么时候、认可了什么（记进 provenance.reviewed_at / reviewed_by）",
    "真实环境证据：在真实 Provider 和真实客户素材上跑过一次的记录",
)


def missing_skills(spec: ProcessSpec, template: WorkflowPackage) -> list[str]:
    """Skills the description names that the template does not carry."""
    named = {str(skill).strip() for step in spec.steps for skill in step.skills if str(skill).strip()}
    return sorted(named - set(template.skills))


def unusable_skill_ids(skill_ids: list[str]) -> list[str]:
    return [skill for skill in skill_ids if not SKILL_ID.match(str(skill or ""))]


def step_for_skill(spec: ProcessSpec, skill_id: str) -> Step | None:
    """The first step that asks for this skill: its words are the draft's words."""
    for step in spec.steps:
        if skill_id in [str(s).strip() for s in step.skills]:
            return step
    return None


def _bullets(items: list[str], empty: str = "无") -> str:
    clean = [str(i).strip() for i in items if str(i).strip()]
    return "\n".join(f"- {i}" for i in clean) if clean else f"- {empty}"


def draft_skill_files(spec: ProcessSpec, step: Step, skill_id: str, *, generated_at: str) -> dict[str, str]:
    """The two files of a drafted skill, keyed by path inside the skill directory."""
    meta: dict[str, Any] = {
        "name": skill_id,
        "description": str(step.responsibility or step.name).strip()[:300],
        "metadata": {
            "version": DRAFT_VERSION,
            "status": "draft",
            "provenance": {
                "source": "process_spec",
                "spec_id": spec.spec_id,
                "spec_version": spec.version,
                "spec_digest": spec.digest(),
                "step_id": step.id,
                "generated_at": generated_at,
                "reviewed_at": "",
                "reviewed_by": "",
            },
            "tests": ["static_frontmatter_contract", "tests/contract.md"],
            "success_criteria": [str(s).strip() for s in step.standard if str(s).strip()],
        },
    }
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    skill_md = (
        f"---\n{front}\n---\n\n"
        f"# {step.name}（草稿）\n\n"
        f"这份技能是从流程描述「{spec.name}」（{spec.spec_id}@{spec.version}）的步骤「{step.name}」生成的。"
        "**没有人评审过，也没有在任何环境跑过。** 它能被候选包加载、拿去离线试跑；"
        "在有人补上方法、评审并记录之前，不能锁定为生产版本。\n\n"
        f"## 职责\n\n{str(step.responsibility or step.name).strip()}\n\n"
        f"## 输入\n\n{_bullets(step.inputs, '描述里没有写')}\n\n"
        f"## 交付物\n\n{_bullets(step.outputs)}\n\n"
        f"## 做成什么样算做完\n\n{_bullets(step.standard)}\n\n"
        f"## 可用的工具\n\n{_bullets(step.tools, '描述里没有指定')}\n\n"
        f"## 锁定发布前还要补的\n\n{_bullets(list(STILL_NEEDED))}\n"
    )
    contract_meta = {
        "date": generated_at[:10],
        "outcome": "unrun",
        "workflow_version": spec.version,
        "notes": "由流程描述生成时写下；这份技能尚未在任何环境跑过，这里不是通过证据。",
    }
    contract_md = (
        f"---\n{yaml.safe_dump(contract_meta, allow_unicode=True, sort_keys=False).strip()}\n---\n\n"
        f"# {skill_id} 验证范围（草稿）\n\n"
        "静态校验只检查这份文件的字段齐全。实盘时逐项检查：\n\n"
        f"{_bullets(step.standard)}\n\n"
        "跑过之后把日期、结果和证据写在这里；没跑过就让它保持 unrun。\n"
    )
    return {"SKILL.md": skill_md, "tests/contract.md": contract_md}


@_guard_manifest_writer
def add_draft_skills(candidate: WorkflowPackage, spec: ProcessSpec, skill_ids: list[str], *,
                     generated_at: str = "", persist_manifest: bool = True) -> WorkflowPackage:
    """Write drafted skills into the candidate's own assets and list them in its manifest.

    Only the candidate's assets are touched: a candidate carries its skills
    under ``assets/skills`` precisely so that a draft can never write into
    the shared skill tree another package depends on.
    """
    ids = [str(s).strip() for s in skill_ids if str(s).strip()]
    if type(persist_manifest) is not bool:
        raise ValueError("persist_manifest must be a boolean")
    if not ids:
        return candidate
    bad = unusable_skill_ids(ids)
    if bad:
        raise ValueError("skill ids must be lowercase letters, digits, '-' or '_' (2-64 chars): " + ", ".join(bad))
    root = candidate.skills_root.resolve()
    try:
        root.relative_to(candidate.root.resolve())
    except ValueError as exc:
        raise ValueError("drafted skills may only be written into the candidate's own assets") from exc
    stamp = generated_at or datetime.now().isoformat(timespec="seconds")
    written: list[str] = []
    for skill_id in ids:
        step = step_for_skill(spec, skill_id)
        if step is None:
            raise ValueError(f"no step of the description asks for skill {skill_id}")
        target = root / skill_id
        if target.exists():
            raise ValueError(f"skill {skill_id} already exists in the candidate; drafts do not overwrite")
        files = draft_skill_files(spec, step, skill_id, generated_at=stamp)
        (target / "tests").mkdir(parents=True)
        for relative, text in files.items():
            (target / relative).write_text(text, encoding="utf-8", newline="\n")
        written.append(skill_id)
    manifest = dict(candidate.manifest)
    manifest["skills"] = [*candidate.skills, *[s for s in written if s not in candidate.skills]]
    provenance = dict(manifest.get("provenance") or {})
    provenance["generated_skills"] = [
        {"id": skill_id, "version": DRAFT_VERSION, "generated_at": stamp, "reviewed": False}
        for skill_id in written
    ]
    manifest["provenance"] = provenance
    # The component-adoption path already owns the original manifest lock
    # and will perform one original node-save CAS. It prepares assets and
    # metadata here without changing that CAS's acknowledged revision.
    updated = (_write_manifest_atomic(candidate, manifest, suffix="skills") if persist_manifest else
        WorkflowPackage(root=candidate.root, manifest=manifest, tests=list(candidate.tests), loaded_revision=candidate.loaded_revision))
    problems = validate_package(updated)
    if problems:
        raise ValueError("candidate with drafted skills does not validate: " + "; ".join(problems))
    return updated


@_guard_manifest_writer
def mark_skill_reviewed(candidate: WorkflowPackage, skill_id: str, *, actor: str, note: str = "",
                        reviewed_at: str = "") -> WorkflowPackage:
    """A person says they reviewed a drafted skill; the file and the manifest record it.

    Only a skill in the package's own assets, only while it says ``status:
    draft``, and only for a package that is not published. The words of the
    skill are not touched -- this records who stood behind them and when,
    which is exactly the fact the loader was refusing to lock without.
    """
    clean_actor = str(actor or "").strip()
    if not clean_actor:
        raise ValueError("a review needs a named reviewer")
    if candidate.status == "published":
        raise ValueError("a published package's skills are locked; review the draft before publication")
    skill = str(skill_id or "").strip()
    if not SKILL_ID.match(skill):
        raise ValueError(f"{skill_id!r} is not a usable skill id")
    root = candidate.skills_root.resolve()
    try:
        root.relative_to(candidate.root.resolve())
    except ValueError as exc:
        raise ValueError("only skills in the package's own assets can be reviewed here") from exc
    path = root / skill / "SKILL.md"
    if not path.is_file():
        raise ValueError(f"skill {skill} is not in this package")
    raw = path.read_text(encoding="utf-8")
    meta, body = _parse_front_matter(raw)
    metadata = dict(meta.get("metadata") or {}) if isinstance(meta, dict) else {}
    if str(metadata.get("status", "") or "").strip().lower() != "draft":
        raise ValueError(f"skill {skill} is not a draft; there is nothing to promote")
    stamp = reviewed_at or datetime.now().isoformat(timespec="seconds")
    provenance = dict(metadata.get("provenance") or {})
    provenance["reviewed_at"] = stamp
    provenance["reviewed_by"] = clean_actor
    if str(note or "").strip():
        provenance["review_note"] = str(note).strip()[:500]
    metadata.pop("status", None)
    metadata["provenance"] = provenance
    new_meta = {**meta, "metadata": metadata}
    front = yaml.safe_dump(new_meta, allow_unicode=True, sort_keys=False).strip()
    path.write_text(f"---\n{front}\n---{body}", encoding="utf-8", newline="\n")
    manifest = dict(candidate.manifest)
    prov = dict(manifest.get("provenance") or {})
    generated = [dict(item) for item in list(prov.get("generated_skills") or [])]
    for item in generated:
        if str(item.get("id", "")) == skill:
            item["reviewed"] = True
            item["reviewed_by"] = clean_actor
            item["reviewed_at"] = stamp
    prov["generated_skills"] = generated
    manifest["provenance"] = prov
    updated = _write_manifest_atomic(candidate, manifest, suffix="review")
    problems = validate_package(updated)
    if problems:
        raise ValueError("package does not validate after the review: " + "; ".join(problems))
    return updated
