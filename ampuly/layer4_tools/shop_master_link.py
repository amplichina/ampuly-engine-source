"""business_id ↔ project_id master-data link (minimal convention).

Store under AMPULY_HOME (not the git repo)::

    AMPULY_HOME/config/business-project-map.json

Sandbox default: ampuly ↔ default (CRM/project workspace already used in practice).
Brand subdirectory can wait; this only links生意 to 项目主档.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ampuly.layer4_tools.shop_workspace import _now, ampuly_home, write_json

MAP_SCHEMA = 1
MAP_REL = Path("config") / "business-project-map.json"
DEFAULT_MAP = {
    "schema_version": MAP_SCHEMA,
    "kind": "ampuly.business_project_map",
    "updated_at": None,
    "mappings": [
        {
            "business_id": "ampuly",
            "project_id": "default",
            "notes": "Sandbox shop shares project default CRM / tasks until brand split",
        }
    ],
}


def map_path(home: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    root = Path(home) if home is not None else ampuly_home(env)
    return root / MAP_REL


def ensure_map(home: Path | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    path = map_path(home, env)
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("business_project_map_not_object")
        return data
    payload = dict(DEFAULT_MAP)
    payload["updated_at"] = _now()
    write_json(path, payload)
    return payload


def show_map(home: Path | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    data = ensure_map(home, env)
    path = map_path(home, env)
    return {
        "ok": True,
        "path": str(path),
        "count": len(data.get("mappings") or []),
        "mappings": list(data.get("mappings") or []),
        "schema_version": data.get("schema_version"),
    }


def resolve_project_id(
    business_id: str,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    *,
    default: str = "default",
) -> dict[str, Any]:
    bid = (business_id or "").strip()
    if not bid:
        return {"ok": False, "error": "business_id_required"}
    data = ensure_map(home, env)
    for row in data.get("mappings") or []:
        if isinstance(row, dict) and str(row.get("business_id") or "").strip() == bid:
            pid = str(row.get("project_id") or "").strip() or default
            return {
                "ok": True,
                "business_id": bid,
                "project_id": pid,
                "notes": row.get("notes"),
                "path": str(map_path(home, env)),
            }
    return {
        "ok": True,
        "business_id": bid,
        "project_id": default,
        "notes": "no explicit mapping; using default",
        "fallback": True,
        "path": str(map_path(home, env)),
    }


def link_business_project(
    business_id: str,
    project_id: str,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    bid = (business_id or "").strip()
    pid = (project_id or "").strip()
    if not bid or not pid:
        return {"ok": False, "error": "business_id_and_project_id_required"}
    data = ensure_map(home, env)
    mappings = [m for m in (data.get("mappings") or []) if isinstance(m, dict)]
    replaced = False
    row = {"business_id": bid, "project_id": pid, "notes": notes or ""}
    for i, existing in enumerate(mappings):
        if str(existing.get("business_id") or "").strip() == bid:
            mappings[i] = {**existing, **row}
            replaced = True
            break
    if not replaced:
        mappings.append(row)
    data["mappings"] = mappings
    data["schema_version"] = MAP_SCHEMA
    data["kind"] = "ampuly.business_project_map"
    data["updated_at"] = _now()
    path = write_json(map_path(home, env), data)
    return {
        "ok": True,
        "path": str(path),
        "business_id": bid,
        "project_id": pid,
        "replaced": replaced,
        "count": len(mappings),
    }


def describe_master_link_capability() -> dict[str, Any]:
    return {
        "id": "shop.business_project_map",
        "title": "business_id ↔ project_id master link",
        "path": MAP_REL.as_posix(),
        "cli": [
            "ampuly shop master show",
            "ampuly shop master link",
            "ampuly shop master resolve",
        ],
        "sandbox_default": {"business_id": "ampuly", "project_id": "default"},
    }
