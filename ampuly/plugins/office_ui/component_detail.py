"""组件 · 查看: one internal-library skill, read only (4.7 Q03, docs/4.7-Q00-CAPABILITY-CARDS.zh.md).

The 内部库 list shows a card per skill; this is what opening one shows: its release contract, where it came from
and who let it in, what it asks of the machine, its files, the SKILL.md text itself, and every step that uses it --
with the version that step pinned and whether the step carries parameters of its own.

It reads files under the shared library and the packages the console already loaded. It never writes, never
downloads, never reaches the network, and never follows a link out of the library.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

MAX_BODY_CHARS = 20000
MAX_FILES = 200


class ComponentNotFound(LookupError):
    """No usable skill of that id in the shared library."""


def _files(folder: Path) -> tuple[list[dict[str, Any]], bool]:
    from ampuly.plugins.office_ui.component_catalog import _is_link

    rows: list[dict[str, Any]] = []
    for current, dirnames, filenames in os.walk(folder, followlinks=False):
        here = Path(current)
        dirnames[:] = sorted(name for name in dirnames if not _is_link(here / name) and name not in {".git", "node_modules"})
        for name in sorted(filenames):
            path = here / name
            if _is_link(path):
                continue
            if len(rows) >= MAX_FILES:
                return rows, True
            try:
                size = path.stat().st_size
            except OSError:
                continue
            rows.append({"path": path.relative_to(folder).as_posix(), "bytes": size})
    return rows, False


def _used_on(skill_id: str, skills_root: Path, packages: list[Any]) -> list[dict[str, Any]]:
    from ampuly.plugins.office_ui.node_components import derived_components

    rows: list[dict[str, Any]] = []
    for package in packages:
        if Path(package.skills_root).resolve() != skills_root:
            continue
        versions = dict(package.manifest.get("skill_versions", {}) or {})
        for node in package.nodes:
            if not isinstance(node, dict):
                continue
            instances = node.get("components") if isinstance(node.get("components"), list) else derived_components(node, versions)
            for item in instances:
                if not isinstance(item, dict) or item.get("kind") != "skill" or item.get("ref") != skill_id:
                    continue
                rows.append({
                    "workflow_id": str(package.id),
                    "workflow_name": str(package.name),
                    "status": str(package.status),
                    "node_id": str(node.get("id", "") or ""),
                    "node_name": str(node.get("name", node.get("id", "")) or ""),
                    "pinned_version": str(item.get("version") or versions.get(skill_id, "") or ""),
                    "has_params": bool(item.get("params")),
                })
    return rows


def skill_detail(skills_root: Path, skill_id: str, packages: list[Any]) -> dict[str, Any]:
    """Everything a person reads before using or changing one shared skill. Raises ComponentNotFound."""
    from ampuly.plugins.office_ui.component_catalog import _frontmatter, _is_link, _license, _requirements, _summary
    from ampuly.plugins.office_ui.workflow_packages import LIBRARY_SKILL_ID, admission_record, load_skill_contract

    root = Path(skills_root).resolve()
    clean = str(skill_id or "").strip()
    if not LIBRARY_SKILL_ID.match(clean):
        raise ComponentNotFound(f"{clean!r} is not a library skill id")
    folder = root / clean
    skill_md = folder / "SKILL.md"
    if _is_link(folder) or _is_link(skill_md) or not skill_md.is_file():
        raise ComponentNotFound(f"the internal library holds no skill {clean!r}")
    try:
        skill_md.resolve().relative_to(root)
    except ValueError as exc:
        raise ComponentNotFound(f"the internal library holds no skill {clean!r}") from exc
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    front = _frontmatter(text) or {}
    contract = load_skill_contract(root, clean)
    files, files_truncated = _files(folder)
    return {
        "kind": "skill",
        "id": clean,
        "name": str(front.get("display_name") or front.get("name") or clean),
        "summary": _summary(front.get("description")),
        "version": contract.version,
        "contract": contract.to_payload(),
        "admission": admission_record(folder),
        "license": _license(front, folder),
        "requires": _requirements(front),
        "files": files,
        "files_truncated": files_truncated,
        "body": text[:MAX_BODY_CHARS],
        "body_truncated": len(text) > MAX_BODY_CHARS,
        "used_on": _used_on(clean, root, packages),
    }
