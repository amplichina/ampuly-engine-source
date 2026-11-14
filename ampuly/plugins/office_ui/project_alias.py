"""Stable project id with an editable display name.

Adopting an AI employee freezes ``project_id`` inside the instance and its
revision chain. Renaming that project must not move the directory or rewrite
those bytes. The id stays. The name the owner typed is the display name, and
an alias so a later read under that name still opens the same instances.
"""
from __future__ import annotations

import json
from pathlib import Path

from ampuly.core.config import replace_with_retry

_FILE = "project_aliases.json"


def alias_path(root: Path) -> Path:
    return Path(root) / _FILE


def load_aliases(root: Path) -> dict[str, str]:
    path = alias_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str) and key and value and key != value:
            out[key] = value
    return out


def resolve_project_alias(root: Path, project_id: str) -> str:
    """One hop. The stored id is never rewritten, and aliases do not chain."""
    name = str(project_id or "")
    target = load_aliases(root).get(name)
    return target if target else name


def register_alias(root: Path, alias: str, project_id: str) -> None:
    """Point ``alias`` at the stable id. Older aliases of that id stay valid."""
    aliases = load_aliases(root)
    aliases[str(alias)] = str(project_id)
    path = alias_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"schema": 1, "aliases": dict(sorted(aliases.items()))}, ensure_ascii=False, indent=2)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(body + "\n", encoding="utf-8")
    replace_with_retry(temporary, path)
