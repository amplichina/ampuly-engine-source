"""Private, read-only candidate bytes; not publication authority or a writer.

Only fixed-host FrozenPublication inputs are accepted. Existing SafeFiles does
all native handle-relative I/O and rejects links/hard links/lexical fallback.
Two bounded passes detect observed changes; they do NOT prove a cross-file
atomic source instant, no ABA, source authorization, or original effect.
No repository, database, root, lock, installation, or recovery is created.
The returned frozen bytes remain private host data, never an RPC receipt.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Any

import yaml

from .ai_employee_contract import (
    AIEmployeeContractError, MAX_FILE_BYTES, MAX_JSON_NODES, MAX_MANIFEST_BYTES, _declarations,
    canonical_json, package_fingerprint, validate_package_manifest,
)
from .ai_employee_safe_files import AIEmployeeSafeFiles, _identity
from .employee_package_versions import _decode
from .employee_packages import _UniqueSafeLoader
from .employee_publication_authority import FrozenPublication, _plan_data
from .employee_publication_contents import publication_content_fingerprint

_MESSAGES = {
    "publication_source_plan_invalid": "读取候选的冻结条件无效；未读取来源。",
    "publication_source_unavailable": "候选来源不可用或不满足安全读取边界。",
    "publication_source_invalid": "来源草稿格式、身份或声明不符合冻结条件。",
    "publication_source_revision_conflict": "来源草稿已不是原冻结修订。",
    "publication_source_content_conflict": "来源原字节内容与冻结候选不一致。",
    "publication_source_changed": "两遍读取之间观察到来源变化；未提供候选字节。",
}


def _fail(code: str) -> None:
    # Never attach input paths, private records, or lower-layer exceptions.
    raise AIEmployeeContractError(code, _MESSAGES[code]) from None


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, repr=False)
class PublicationSourceFile:
    path: str
    raw: bytes


@dataclass(frozen=True, repr=False)
class PublicationSourceSnapshot:
    request_key: str
    plan_sha256: str
    package_id: str
    version: str
    fingerprint: str
    source_draft_revision: str
    manifest_raw: bytes
    files: tuple[PublicationSourceFile, ...]


class _BoundedSourceLoader(_UniqueSafeLoader):
    """Existing alias/depth checks, plus a ceiling before allocating all nodes."""

    def compose_node(self, parent: Any, index: Any) -> yaml.Node:
        self._source_nodes = getattr(self, "_source_nodes", 0) + 1
        if self._source_nodes > MAX_JSON_NODES:
            raise ValueError("source YAML node limit")
        return super().compose_node(parent, index)


def _preflight(root: Path, plan: FrozenPublication) -> tuple[dict, dict, str]:
    try:
        # Full authority/namespace/type/size preflight precedes ANY workspace
        # creation/open. A FrozenPublication constructor is not authorization.
        data = _plan_data(plan)
        if (data["request"]["action"] != "employee_package_publish"
                or not isinstance(root, Path) or not root.is_absolute()):
            _fail("publication_source_plan_invalid")
        candidate = _decode(plan.manifest_raw, "manifest")
        return data, candidate, plan.request.key()
    except Exception:
        _fail("publication_source_plan_invalid")


def _source_manifest(raw: bytes, data: dict, candidate: dict) -> dict:
    if type(raw) is not bytes or len(raw) > MAX_MANIFEST_BYTES:
        _fail("publication_source_invalid")
    if _sha(raw) != data["source_draft_revision"]:
        _fail("publication_source_revision_conflict")
    try:
        # Match EmployeePackageRepository._load: duplicate/alias/depth-safe
        # YAML, strict UTF-8, then existing bounded canonical JSON shape.
        source = yaml.load(raw.decode("utf-8"), Loader=_BoundedSourceLoader)
        canonical_json(source)
        if (type(source) is not dict
                or type(source.get("schema_version")) is not int
                or source["schema_version"] != 1
                or source.get("status") not in {"draft", "testing"}
                or source.get("package_id") != data["package_id"]
                or source.get("version") != data["version"]
                or source.get("skills") != candidate.get("skills")
                or _declarations(source) != _declarations(candidate)
                or any(p["severity"] == "error" for p in validate_package_manifest(source))):
            _fail("publication_source_invalid")
    except Exception:
        _fail("publication_source_invalid")
    if source["package_fingerprint"] != data["fingerprint"]:
        _fail("publication_source_content_conflict")
    return source


def _workspace_identity(workspace: Any) -> tuple[int, int]:
    if os.name == "nt":
        return workspace._identity(workspace.root_handle)
    return _identity(workspace.root_fd)


def _required_read(files: AIEmployeeSafeFiles, relative: str, limit: int) -> bytes:
    raw = files.read(relative, limit)
    if type(raw) is not bytes:
        _fail("publication_source_unavailable")
    return raw


def _read_files(files: AIEmployeeSafeFiles, prefix: str, data: dict,
                previous: dict[str, bytes] | None = None) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    total = 0
    # SafeFiles requires a positive limit even for an empty regular file. Read
    # expected-empty files first, so a full payload budget never leaves a later
    # empty-file probe. Existing SafeFiles bounds each read (including growth).
    for row in sorted(data["files"], key=lambda r: (r["size"] != 0, r["path"])):
        raw = _required_read(files, prefix + "/" + row["path"], max(1, row["size"]))
        total += len(raw)
        if total > MAX_FILE_BYTES:
            _fail("publication_source_content_conflict")
        if previous is not None and raw != previous[row["path"]]:
            _fail("publication_source_changed")
        if len(raw) != row["size"] or _sha(raw) != row["sha256"]:
            _fail("publication_source_content_conflict")
        result[row["path"]] = raw
    return result


def read_publication_source_snapshot(root: Path, plan: FrozenPublication) -> PublicationSourceSnapshot:
    """Read two matching passes of the original draft and declared raw files.

    `root` and `plan` must originate in fixed authenticated host code. This
    function neither authenticates them nor checks a publication marker/lease.
    Never pass this value alone as permission or a native promotion witness.
    """
    data, candidate, request_key = _preflight(root, plan)
    plan_sha256 = _sha(canonical_json(data))
    if candidate.get("skills"):
        # Inventory complete trees, not merely expected leaves: an unlisted
        # late-added file must never disappear from the frozen content check.
        from .employee_publication_candidate import _collect_publication_candidate
        raw, contents, _trees = _collect_publication_candidate(root, candidate, plan.source_draft_revision)
        expected = {row["path"]: row for row in data["files"]}
        if (list(contents) != list(expected)
                or any(len(contents[path]) != row["size"] or _sha(contents[path]) != row["sha256"]
                       for path, row in expected.items())):
            _fail("publication_source_content_conflict")
        return PublicationSourceSnapshot(request_key, plan_sha256, plan.package_id, plan.version,
                                         plan.fingerprint, plan.source_draft_revision, raw,
                                         tuple(PublicationSourceFile(path, body) for path, body in contents.items()))
    prefix = "employee_packages/" + data["package_id"]
    manifest_path = prefix + "/manifest.yaml"
    files = AIEmployeeSafeFiles(root)
    body_error: BaseException | None = None

    def remember_error(error: BaseException) -> None:
        nonlocal body_error
        if body_error is None or isinstance(body_error, Exception) and not isinstance(error, Exception):
            body_error = error

    try:
        with files.workspace() as pinned:
            try:
                original_root = _workspace_identity(pinned)

                def guard(workspace: Any) -> None:
                    try:
                        if _workspace_identity(workspace) != original_root:
                            _fail("publication_source_changed")
                    except BaseException as exc:
                        remember_error(exc)
                        raise

                # Keep the outer native root open across both passes. Each
                # SafeFiles.read checks the same root before and after its I/O.
                files.root_guard = guard
                manifest_raw = _required_read(files, manifest_path, MAX_MANIFEST_BYTES)
                source = _source_manifest(manifest_raw, data, candidate)
                first = _read_files(files, prefix, data)
                try:
                    if (publication_content_fingerprint(source, first) != data["fingerprint"]
                            or publication_content_fingerprint(candidate, first) != data["fingerprint"]):
                        _fail("publication_source_content_conflict")
                except AIEmployeeContractError as exc:
                    if exc.code == "publication_source_content_conflict":
                        raise
                    _fail("publication_source_invalid")
                if _required_read(files, manifest_path, MAX_MANIFEST_BYTES) != manifest_raw:
                    _fail("publication_source_changed")
                _read_files(files, prefix, data, first)
            except BaseException as exc:
                remember_error(exc)
                raise
        # A context exit that suppresses a body/guard failure cannot turn a
        # late failed second pass into success merely because first is filled.
        if body_error is not None:
            raise body_error from None
        # Only return after SafeFiles' final named-root identity verification.
        return PublicationSourceSnapshot(
            request_key=request_key, plan_sha256=plan_sha256,
            package_id=data["package_id"], version=data["version"],
            fingerprint=data["fingerprint"], source_draft_revision=data["source_draft_revision"],
            manifest_raw=manifest_raw,
            files=tuple(PublicationSourceFile(path, first[path]) for path in sorted(first)),
        )
    except BaseException as exc:
        # Preserve cancellation originating in the body/our guard if outer
        # context cleanup replaces it. A new cleanup cancellation still wins
        # over an ordinary body error. No claim is made about cancellation
        # already masked inside a shared SafeFiles.read/native primitive.
        if body_error is not None and not isinstance(body_error, Exception):
            raise body_error from None
        if not isinstance(exc, Exception):
            raise
        error = body_error if body_error is not None else exc
        if isinstance(error, AIEmployeeContractError) and error.code in _MESSAGES:
            _fail(error.code)
        _fail("publication_source_unavailable")
