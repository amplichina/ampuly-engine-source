"""Fixed-host, read-only bounded candidate collector; never authorization.

Reads only manifest-declared resources and package skill roots. Preserve legacy
native traversal order, including at the returned dictionary boundary. Two
matching passes detect observed changes, not an atomic or ABA-proof snapshot.
"""
from __future__ import annotations

from pathlib import Path
import os
import json

from .ai_employee_contract import AIEmployeeContractError, MAX_FILE_BYTES, MAX_MANIFEST_BYTES, canonical_json, validate_package_manifest
from .ai_employee_safe_files import AIEmployeeSafeFiles, validate_relative
from .employee_publication_contents import (
    MAX_PUBLICATION_DEPTH, MAX_PUBLICATION_ENTRIES, MAX_PUBLICATION_FILES,
    MAX_PUBLICATION_PATH_BYTES, MAX_SKILL_FILE_BYTES, bounded_publication_paths,
    package_skill_fingerprints, publication_content_fingerprint, publication_layout,
)
from .employee_publication_source_snapshot import _source_manifest, _workspace_identity, _required_read


def _fail(code: str = "publication_candidate_invalid") -> None:
    raise AIEmployeeContractError(code, "候选资源或技能整树尚不能安全冻结。") from None


def _read_pass(files, pinned, prefix: str, manifest: dict, remaining: int):
    layout = publication_layout(manifest)
    result: dict[str, bytes] = {}
    inventories = {}
    entries = 0

    def read(path, *, skill=False):
        nonlocal remaining
        if path in result or len(result) >= MAX_PUBLICATION_FILES - 1:
            _fail()
        maximum = min(MAX_SKILL_FILE_BYTES if skill else MAX_FILE_BYTES, remaining)
        raw = _required_read(files, prefix + "/" + path, max(1, maximum))
        remaining -= len(raw)
        if remaining < 0:
            _fail()
        result[path] = raw

    for row in layout["declared_files"]:
        read(row["source_ref"])
    for skill in layout["skill_trees"]:
        pending = [skill["source_ref"]]
        while pending:
            folder = pending.pop()
            # Depth includes the prospective file. Never enumerate past bound.
            if len(folder.split("/")) >= MAX_PUBLICATION_DEPTH:
                _fail()
            target = files.target(pinned, prefix + "/" + folder)
            limit = MAX_PUBLICATION_ENTRIES - entries + 1
            children = (files._list_windows(pinned, target, limit) if os.name == "nt"
                        else files._list_posix(pinned, target, limit))
            entries += len(children)
            if not children or entries > MAX_PUBLICATION_ENTRIES:
                _fail()  # Empty directory has no frozen file representation.
            inventories[folder] = tuple((row["name"], row["kind"]) for row in children)
            subdirectories = []
            for row in children:
                if type(row) is not dict or set(row) != {"name", "kind"} or type(row["name"]) is not str:
                    _fail()
                name = row["name"]
                if "/" in name or "\\" in name or any(character in name for character in '<>"|?*'):
                    _fail()
                relative = folder + "/" + name
                parts = validate_relative(prefix + "/" + relative)[2:]
                if len(parts) > MAX_PUBLICATION_DEPTH or len(relative.encode("utf-8")) > MAX_PUBLICATION_PATH_BYTES:
                    _fail()
                if row["kind"] == "directory":
                    subdirectories.append(relative)
                elif row["kind"] == "file":
                    read(relative, skill=True)
                else:
                    _fail()
            pending.extend(reversed(subdirectories))  # Exactly os.walk's DFS.
    bounded_publication_paths(manifest, result)
    return result, inventories


def _collect_publication_candidate(root: Path, manifest: dict, expected_draft_revision: str):
    # Complete declared-path preflight before opening the root. No caller path
    # or library source can expand the read area outside these package roots.
    if (not isinstance(root, Path) or not root.is_absolute() or type(manifest) is not dict
            or type(expected_draft_revision) is not str or len(expected_draft_revision) != 64
            or any(c not in "0123456789abcdef" for c in expected_draft_revision)
            or any(row["severity"] == "error" for row in validate_package_manifest(manifest))):
        _fail()
    manifest = json.loads(canonical_json(manifest))
    layout = publication_layout(manifest)
    bounded_publication_paths(manifest, [row["source_ref"] for row in layout["declared_files"]]
                              + [row["source_ref"] + "/SKILL.md" for row in layout["skill_trees"]])
    prefix = "employee_packages/" + manifest["package_id"]
    data = {"package_id": manifest["package_id"], "version": manifest["version"],
            "fingerprint": manifest["package_fingerprint"], "source_draft_revision": expected_draft_revision}
    files = AIEmployeeSafeFiles(root)
    body_error = None
    try:
        with files.workspace() as pinned:
            try:
                original = _workspace_identity(pinned)

                def guard(workspace):
                    if _workspace_identity(workspace) != original:
                        _fail("publication_source_changed")

                files.root_guard = guard
                raw = _required_read(files, prefix + "/manifest.yaml", MAX_MANIFEST_BYTES)
                source = _source_manifest(raw, data, manifest)
                remaining = MAX_FILE_BYTES - max(len(raw), len(canonical_json(manifest)))
                first, inventory = _read_pass(files, pinned, prefix, manifest, remaining)
                if (publication_content_fingerprint(source, first) != data["fingerprint"]
                        or publication_content_fingerprint(manifest, first) != data["fingerprint"]):
                    _fail("publication_source_content_conflict")
                if _required_read(files, prefix + "/manifest.yaml", MAX_MANIFEST_BYTES) != raw:
                    _fail("publication_source_changed")
                second, second_inventory = _read_pass(files, pinned, prefix, manifest, remaining)
                if list(first.items()) != list(second.items()) or inventory != second_inventory:
                    _fail("publication_source_changed")
                trees = package_skill_fingerprints(manifest, second)
            except BaseException as exc:
                body_error = exc
                raise
        if body_error is not None:
            raise body_error from None
        return raw, first, trees
    except BaseException as exc:
        if body_error is not None and not isinstance(body_error, Exception):
            raise body_error from None
        if not isinstance(exc, Exception):
            raise
        error = body_error if body_error is not None else exc
        if isinstance(error, AIEmployeeContractError):
            _fail(error.code)
        _fail("publication_source_unavailable")


def collect_publication_candidate(root: Path, manifest: dict, expected_draft_revision: str) -> tuple[dict[str, bytes], dict[str, str]]:
    """Host-private bytes and verified skill hashes. Keep returned dict order.

    Neither this read nor its return value is readiness, owner consent, an
    immutable artifact, or authority to publish/execute any component.
    """
    _raw, files, trees = _collect_publication_candidate(root, manifest, expected_draft_revision)
    return files, trees
