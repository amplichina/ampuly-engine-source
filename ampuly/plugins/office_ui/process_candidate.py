"""Compile a saved ProcessSpec using an existing package's roles and skills.

This is the template-reuse path, not a new Skill generator or an execution
engine. It creates a draft through the existing package writer; installation,
tool grants, trials and release remain separate owner-controlled operations.
"""
from __future__ import annotations

import copy
import os
import shutil
from pathlib import Path

from ampuly.plugins.office_ui.process_spec import ProcessSpec, compile_to_workflow
from ampuly.plugins.office_ui.workflow_packages import (
    INTAKE_FILLED_KEYS,
    WorkflowPackage,
    create_workflow_draft,
    draft_manifest_from,
    validate_package,
)


def candidate_template_catalog(packages: list[WorkflowPackage]) -> list[dict]:
    """Owner-facing metadata only; availability is not approval or compatibility."""
    return [{"id": package.id, "name": package.name,
             "version": str(package.manifest.get("version", "")),
             "status": package.status, "valid": not package.problems,
             "skill_count": len(package.skills)}
            for package in packages if package.status in {"draft", "testing", "published"}]


def _linked(path: Path) -> bool:
    # Path.is_junction only exists from Python 3.12; the project supports
    # 3.10 too. Windows lstat exposes reparse attributes without following.
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def _bounded_assets(template: WorkflowPackage) -> None:
    """Refuse links or unexpectedly large template trees before clone copies."""
    count = size = 0
    roots = [template.org_config_path]
    for skill in template.skills:
        path = template.skills_root / skill
        if not path.resolve().is_relative_to(template.skills_root.resolve()):
            raise ValueError("skill escapes template assets")
        roots.append(path)
    for root in roots:
        if root is None or not root.exists():
            raise ValueError("template assets are missing")
        if _linked(root):
            raise ValueError("linked template assets are not supported")
        if root.is_file():
            count += 1
            size += root.stat().st_size
        else:
            def denied(error: OSError) -> None:
                raise ValueError("template assets cannot be completely inspected") from error
            for parent, directories, files in os.walk(root, followlinks=False, onerror=denied):
                for name in directories + files:
                    path = Path(parent) / name
                    if _linked(path):
                        raise ValueError("linked template assets are not supported")
                    count += 1
                    if path.is_file():
                        size += path.stat().st_size
                    if count > 2000 or size > 32 * 1024 * 1024:
                        raise ValueError("template exceeds bounded candidate copy (2000 entries / 32 MiB)")
        if count > 2000 or size > 32 * 1024 * 1024:
            raise ValueError("template exceeds bounded candidate copy (2000 entries / 32 MiB)")


def create_process_candidate(spec: ProcessSpec, template: WorkflowPackage, *, ampuly_home: Path,
                             draft_id: str, actor: str, generate_missing_skills: bool = False) -> WorkflowPackage:
    """Produce a reloadable draft, or refuse before creating any directory.

    With ``generate_missing_skills`` the skills the description names and
    the template lacks are drafted into the candidate's own assets, each
    marked as unreviewed; without it they are a refusal, as before.
    """
    from ampuly.plugins.office_ui.skill_factory import add_draft_skills, missing_skills, unusable_skill_ids

    if template.status == "retired":
        raise ValueError("retired templates cannot seed a new process candidate")
    workflow = compile_to_workflow(spec, org_id=template.org_id)
    missing = missing_skills(spec, template)
    if missing and not generate_missing_skills:
        raise ValueError("template is missing skills: " + ", ".join(missing))
    if missing and unusable_skill_ids(missing):
        raise ValueError("skill ids must be lowercase letters, digits, '-' or '_' (2-64 chars): "
                         + ", ".join(unusable_skill_ids(missing)))

    # Reuse known assets, but not a template's grants, customer defaults or
    # publication/acceptance facts: the same list every draft cut from another
    # flow drops (workflow_packages.draft_manifest_from, 4.7 Q01).
    manifest = draft_manifest_from(template.manifest)
    manifest.update({"status": "draft", "nodes": workflow["nodes"],
                     "acceptance": list(spec.acceptance_criteria),
                     "process_constraints": workflow["process_constraints"],
                     "provenance": {**workflow["provenance"], "template_id": template.id,
                                    "template_version": str(template.manifest.get("version", ""))}})
    # The description's own fields are cleared the same way as the template's.
    fields = copy.deepcopy(spec.intake_fields or template.intake_fields)
    for item in fields:
        for key in INTAKE_FILLED_KEYS:
            item.pop(key, None)
    # Use the runtime's existing {field} template protocol, without retaining
    # source-specific prose or hidden defaults from a previous customer's pack.
    for item in fields:
        key = str(item.get("id", ""))
        if not key or not all(c.isalnum() or c == "_" for c in key):
            raise ValueError("candidate intake field needs a safe id")
        item["id"] = key
    manifest["intake"] = {"fields": fields, "template": "\n".join(
        str(item.get("label", item["id"])).replace("{", "{{").replace("}", "}}") + ": {" + item["id"] + "}" for item in fields)}
    prepared = WorkflowPackage(root=template.root, manifest=manifest)
    problems = validate_package(prepared)
    if problems:
        raise ValueError("candidate does not match template: " + "; ".join(problems))
    _bounded_assets(prepared)
    created = create_workflow_draft(prepared, ampuly_home=ampuly_home, draft_id=draft_id,
                                    actor=actor, name=spec.name, summary=spec.trigger,
                                    change_note=f"ProcessSpec {spec.spec_id}@{spec.version} sha256:{spec.digest()}")
    if not missing:
        return created
    try:
        return add_draft_skills(created, spec, missing)
    except Exception:
        # The candidate directory did not exist before this call; a half
        # drafted one must not be discoverable as a package.
        shutil.rmtree(created.root, ignore_errors=True)
        raise
