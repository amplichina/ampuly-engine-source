"""Pure bounded publication contents; no I/O and no publication authority.

Skill digests intentionally retain the existing native walk order. Sorting
skill files here would silently redefine previously reviewed asset identities.
"""
from __future__ import annotations

import hashlib
from typing import Iterable

from .ai_employee_contract import AIEmployeeContractError, MAX_FILE_BYTES, _declarations, _scan_file_text, canonical_json, package_fingerprint
from .ai_employee_safe_files import validate_relative
from .employee_publication_namespace import validate_publication_layout

MAX_PUBLICATION_FILES = 64  # Includes manifest.yaml.
MAX_PUBLICATION_DEPTH = 4  # Components below the package root, including leaf.
MAX_PUBLICATION_PATH_BYTES = 256
MAX_SKILL_FILE_BYTES = 2 * 1024 * 1024  # Matches immutable EmployeeSkillTreeReader.
MAX_PUBLICATION_ENTRIES = MAX_PUBLICATION_FILES * MAX_PUBLICATION_DEPTH


def _fail(code: str = "publication_contents_invalid") -> None:
    raise AIEmployeeContractError(code, "发布内容不满足完整技能树或有界资源约束。") from None


def publication_layout(manifest: dict) -> dict:
    layout = validate_publication_layout(manifest, package_id=manifest.get("package_id"),
                                         fingerprint=manifest.get("package_fingerprint"))
    if any(row["source"] != "package" for row in layout["skill_trees"]):
        # Never erase a library declaration or silently copy an unpinned source.
        _fail("publication_library_skill_source_unavailable")
    return layout


def bounded_publication_paths(manifest: dict, paths: Iterable[str]) -> tuple[str, ...]:
    """Validate all targets before I/O; preserve native order inside each skill."""
    layout = publication_layout(manifest)
    paths = tuple(paths)
    if (any(type(path) is not str for path in paths)
            or len(paths) >= MAX_PUBLICATION_FILES or len(set(paths)) != len(paths)):
        _fail()
    declared = {row["source_ref"] for row in layout["declared_files"]}
    trees = [row["source_ref"] + "/" for row in layout["skill_trees"]]
    namespaces: dict[str, tuple[str, str]] = {"manifest.yaml": ("manifest.yaml", "file")}
    for path in paths:
        if type(path) is not str or len(path.encode("utf-8")) > MAX_PUBLICATION_PATH_BYTES:
            _fail()
        if any(character in path for character in '<>"|?*'):
            _fail()
        parts = validate_relative("employee_packages/" + manifest["package_id"] + "/" + path)[2:]
        if not 1 <= len(parts) <= MAX_PUBLICATION_DEPTH:
            _fail()
        if path not in declared and not any(path.startswith(tree) for tree in trees):
            _fail()
        for index in range(1, len(parts) + 1):
            name = "/".join(parts[:index])
            current = (name, "file" if index == len(parts) else "directory")
            if namespaces.get(name.casefold(), current) != current:
                _fail()
            namespaces[name.casefold()] = current
    if not declared.issubset(paths):
        _fail()
    for tree in trees:
        if tree + "SKILL.md" not in paths:
            _fail()
    # The ordinary flat package remains byte-identical to its old sorted plan.
    return tuple(sorted(declared)) + tuple(path for tree in trees for path in paths if path.startswith(tree))


def package_skill_fingerprints(manifest: dict, files: dict[str, bytes]) -> dict[str, str]:
    ordered = bounded_publication_paths(manifest, files)
    result = {}
    for skill in publication_layout(manifest)["skill_trees"]:
        prefix = skill["source_ref"] + "/"
        rows = []
        for path in ordered:
            if path.startswith(prefix):
                raw = files[path]
                if type(raw) is not bytes or len(raw) > MAX_SKILL_FILE_BYTES:
                    _fail()
                if path == prefix + "SKILL.md" and not raw:
                    _fail()
                try:
                    text = raw.decode("utf-8")
                except UnicodeError:
                    text = ""
                if text:
                    _scan_file_text(text, path)
                rows.append([path[len(prefix):], hashlib.sha256(raw).hexdigest()])
        # Same compact UTF-8 JSON rows as skill_replacement/EmployeeSkillTreeReader.
        digest = hashlib.sha256(canonical_json(rows)).hexdigest()
        if digest != skill["tree_fingerprint"]:
            _fail("publication_skill_tree_conflict")
        result[skill["skill_name"]] = digest
    return result


def publication_content_fingerprint(manifest: dict, files: dict[str, bytes]) -> str:
    if type(files) is not dict or any(type(raw) is not bytes for raw in files.values()):
        _fail()
    if sum(map(len, files.values())) + len(canonical_json(manifest)) > MAX_FILE_BYTES:
        _fail()
    package_skill_fingerprints(manifest, files)
    # The base contract accepts only declared resources. Skills are independently
    # checked above, then bound by their existing manifest tree fingerprints.
    declared = _declarations(manifest)
    return package_fingerprint(manifest, {path: files[path] for path in declared})
