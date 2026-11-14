"""流程版本面板 (4.7 B batch): one flow's versions and which project runs which, read from what is already authoritative.

- current: the package's own manifest -- its version, status, when and by whom it last changed, and why;
- published: every version this package has published. Publishing leaves the manifest as it was signed in
  ``.releases/<version>-<hash>.yaml`` (the rollback point), one per publication; a package that is published now adds
  its own version with the publication record;
- adoptions: every project's adoption of this flow (the business store), each marked when it is on an older version
  than the latest published one. Upgrading is the project's own step in 项目中心, never taken from here;
- differences: each published version against the one before it, the saved manifest against the latest published one
  (including the same version edited in place), and for a project behind, what upgrading would bring -- by step and
  field name only; plus the skill files whose content changed since the latest publication signed them.

Read-only: nothing here writes, and no second record of versions is kept.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _version_key(version: str) -> tuple[int, ...] | None:
    match = _SEMVER.match(str(version or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def is_older(version: str, than: str) -> bool:
    """Is ``version`` behind ``than``? Numbered versions compare as numbers; anything else only by being different."""
    mine, theirs = _version_key(version), _version_key(than)
    if mine is not None and theirs is not None:
        return mine < theirs
    return bool(than) and str(version or "").strip() != str(than).strip()


def _snapshots(package: Any) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any] | None]]:
    """Each published version's entry and, when it can be read, the manifest as it was signed (one per version).

    Several snapshots of one version (a rollback and a republish write a new ``<version>-<hash>.yaml``): the one the
    current publication record names wins, otherwise the first by file name.
    """
    entries: dict[str, dict[str, Any]] = {}
    manifests: dict[str, dict[str, Any] | None] = {}
    publication = dict(package.manifest.get("publication", {}) or {})
    preferred = str(publication.get("rollback_snapshot", "") or "").replace("\\", "/").rsplit("/", 1)[-1]
    folder = package.root / ".releases"
    if folder.is_dir():
        for path in sorted(folder.glob("*.yaml")):
            entry: dict[str, Any] = {"snapshot": path.name, "version": "", "change_note": "", "updated_at": "", "published_at": "", "published_by": ""}
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError):
                data = None
            if not isinstance(data, dict):
                entry["unreadable"] = True
                entry["version"] = path.stem.rsplit("-", 1)[0]
                data = None
            else:
                entry["version"] = str(data.get("version", "") or "")
                entry["change_note"] = str(data.get("change_note", "") or "")
                entry["updated_at"] = str(data.get("updated_at", "") or "")
            version = entry["version"]
            if version in entries and path.name != preferred:
                continue
            entries[version] = entry
            manifests[version] = data
    signed = str(publication.get("evidence_workflow_version", "") or publication.get("rollback_workflow_version", "") or "")
    if publication.get("published_at"):
        version = signed or str(package.manifest.get("version", "") or "")
        if version not in entries:
            entries[version] = {
                "snapshot": "", "version": version, "change_note": str(package.manifest.get("change_note", "") or ""),
                "updated_at": "", "published_at": "", "published_by": "",
            }
            # No snapshot on disk: the published package itself is that version, as long as it still says so.
            same = str(package.manifest.get("version", "") or "") == version
            manifests[version] = dict(package.manifest) if same else None
        entries[version]["published_at"] = str(publication.get("published_at", "") or "")
        entries[version]["published_by"] = str(publication.get("published_by", "") or "")
    return entries, manifests


def _newest_first(entries: Any) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda item: (_version_key(item["version"]) or (-1,), item["version"]), reverse=True)


def release_history(package: Any) -> list[dict[str, Any]]:
    """The versions this package has published, newest first. A snapshot that cannot be read is listed as such."""
    entries, _ = _snapshots(package)
    return _newest_first(entries.values())


# What a version comparison leaves out: when and by whom, the release's own paperwork, and where steps sit on the
# canvas. Everything else in the manifest is the flow's contract -- the same rule the save uses to demand a new
# version (workflow_packages.preview_package_nodes). A denylist on purpose: a new contract key shows up as a change.
def _bookkeeping() -> frozenset[str]:
    from ampuly.plugins.office_ui.workflow_packages import _RELEASE_VOLATILE_KEYS, BUILDER_DECLARED_KEY

    return _RELEASE_VOLATILE_KEYS | {
        "version", "status", "change_note", "updated_at", "updated_by", "created_at", "created_by",
        "testing_submitted_at", "testing_submitted_by", "rollback", "retirement", "approval_records", BUILDER_DECLARED_KEY,
    }


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _same(before: Any, after: Any) -> bool:
    return (_empty(before) and _empty(after)) or before == after


def _node_view(raw: dict[str, Any]) -> dict[str, Any]:
    """A node as a version compares it: every key it carries (the runtime also reads keys the canvas never shows,
    such as revision_path and branch_when) with the console's defaults for the ones it knows, and no position."""
    from ampuly.plugins.office_ui.workflow_packages import node_payload

    shown = {key: value for key, value in node_payload(raw).items() if key != "canvas"}
    return {**{key: value for key, value in raw.items() if key != "canvas"}, **shown}


def manifest_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """What changed from one manifest to another: steps added, removed and changed (by field), and flow-level keys.

    Ids, names and field names only -- never values -- so nothing long or private leaves the package. A key missing on
    one side and empty on the other is the same; a step only moved on the canvas is not a change.
    """
    def nodes_of(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(node.get("id", "")): node for node in list(manifest.get("nodes", []) or []) if isinstance(node, dict) and node.get("id")}

    def named(node_id: str, node: dict[str, Any]) -> dict[str, str]:
        return {"id": node_id, "name": str(node.get("name", "") or node_id)}

    old, new = nodes_of(before), nodes_of(after)
    changed = []
    for node_id, node in new.items():
        if node_id not in old:
            continue
        a, b = _node_view(old[node_id]), _node_view(node)
        fields = sorted(key for key in set(a) | set(b) if not _same(a.get(key), b.get(key)))
        if fields:
            changed.append({**named(node_id, node), "fields": fields})
    skip = _bookkeeping() | {"nodes"}
    return {
        "nodes": {
            "added": [named(node_id, node) for node_id, node in new.items() if node_id not in old],
            "removed": [named(node_id, node) for node_id, node in old.items() if node_id not in new],
            "changed": changed,
        },
        "package": sorted(key for key in set(before) | set(after) if key not in skip and not _same(before.get(key), after.get(key))),
    }


def _any_change(diff: dict[str, Any]) -> bool:
    nodes = diff.get("nodes", {})
    return bool(nodes.get("added") or nodes.get("removed") or nodes.get("changed") or diff.get("package"))


def _compare(label_from: str, before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        # An unreadable side never passes for "nothing changed".
        return {"from": label_from, "readable": False}
    return {"from": label_from, "readable": True, **manifest_diff(before, after)}


def _skills_changed_since_publication(package: Any) -> list[str] | None:
    """Skill files whose content differs from what the latest publication signed; None when nothing was signed."""
    publication = dict(package.manifest.get("publication", {}) or {})
    signed = dict(publication.get("evidence_skill_sha256", {}) or {})
    if not publication.get("published_at") or not signed:
        return None
    try:
        now = dict(package.release_evidence.get("skill_sha256", {}) or {})
    except (OSError, ValueError):
        return None
    return sorted(skill for skill in set(signed) | set(now) if signed.get(skill, "") != now.get(skill, ""))


def version_record(package: Any, adoptions: list[dict[str, Any]]) -> dict[str, Any]:
    entries, manifests = _snapshots(package)
    published = _newest_first(entries.values())
    latest = published[0]["version"] if published else ""
    for index, entry in enumerate(published):
        older = published[index + 1] if index + 1 < len(published) else None
        entry["changes_from_previous"] = (
            _compare(older["version"], manifests.get(older["version"]), manifests.get(entry["version"])) if older else None
        )
    manifest = package.manifest
    current_version = str(manifest.get("version", "") or "")
    current_diff = None
    if latest:
        current_diff = _compare(latest, manifests.get(latest), dict(manifest))
        current_diff["same_version"] = current_version == latest
        current_diff["edited_in_place"] = bool(current_diff["same_version"] and current_diff.get("readable") and _any_change(current_diff))
    rows = []
    for row in adoptions:
        version = str(row.get("workflow_version", "") or "")
        behind = bool(latest) and is_older(version, latest)
        rows.append({
            "project_id": str(row.get("project_id", "") or ""),
            "version": version,
            "status": str(row.get("status", "") or ""),
            "adopted_at": str(row.get("adopted_at", "") or ""),
            "updated_at": str(row.get("updated_at", "") or ""),
            "behind": behind,
            # 升级影响: what that project would take on by moving to the latest published version.
            "upgrade_diff": _compare(version, manifests.get(version), manifests.get(latest)) if behind else None,
        })
    return {
        "workflow_id": package.id,
        "current": {
            "version": current_version,
            "status": package.status,
            "updated_at": str(manifest.get("updated_at", "") or ""),
            "updated_by": str(manifest.get("updated_by", "") or ""),
            "change_note": str(manifest.get("change_note", "") or ""),
        },
        "published": published,
        "latest_published": latest,
        "current_diff": current_diff,
        "skills_changed_since_publication": _skills_changed_since_publication(package),
        "adoptions": rows,
    }
