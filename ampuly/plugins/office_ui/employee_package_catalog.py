"""Bounded read-only published catalogue over native safe files.

No root, lock, staging directory, database or runtime is created. Visibility is
only the validated versions.jsonl index, never the owner draft or an unindexed
immutable directory. Optional anchors are server-owned independent evidence;
hashing this same index cannot establish that it is the current index.

Every request pins its configured root, freezes each index and anchor for the
ImmutableVersionReader, and rereads them before returning. This detects observed
changes, not an atomic filesystem transaction or an ABA-proof global inventory.
Only a current externally anchored index can enable a new-adoption policy flag.
"""
from __future__ import annotations

import hashlib
import os
import re
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from ampuly.layer4_tools.workspace_fs import WorkspaceBoundaryError
from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .ai_employee_safe_files import AIEmployeeSafeFiles
from .employee_package_versions import ImmutableVersionReader, MAX_LEDGER_BYTES, VersionCatalog

MAX_PACKAGES = 500
MAX_REQUEST_READ_BYTES = 64 * 1024 * 1024
MAX_REQUEST_READS = 4096
MAX_REQUEST_TREE_CHECKS = 1000
MAX_LIST_VIEW_BYTES = 2 * 1024 * 1024
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_READ_ACTIONS = ["employee_package_list", "employee_package_detail"]
_STORE_ENTRIES = {".write.lock": "file", ".pending.json": "file", ".requests": "directory", ".lifecycle": "directory"}


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_digest(value: Any) -> str:
    return _digest(canonical_json(value))


def _package_id(value: Any) -> str:
    if type(value) is not str or not _PACKAGE.fullmatch(value):
        raise AIEmployeeContractError("request_invalid", "岗位包编号无效。")
    return value


def _root_identity(workspace: Any) -> tuple[int, int]:
    if os.name == "nt":
        return workspace._identity(workspace.root_handle)
    info = os.fstat(workspace.root_fd)
    return int(info.st_dev), int(info.st_ino)


def _changed() -> None:
    raise AIEmployeeContractError(
        "storage_record_invalid", "读取期间发布依据发生变化，快照一致性未知。",
        details={"read_consistency": "unknown", "effects_produced": False},
    )


class _ReadSnapshot:
    """Small request-local state; native containment remains in SafeFiles."""
    def __init__(self, owner: "PublishedEmployeeCatalog", workspace: Any) -> None:
        self.owner = owner
        identity = _root_identity(workspace)

        def guard(current: Any) -> None:
            if _root_identity(current) != identity:
                _changed()

        self.files = AIEmployeeSafeFiles(owner.root, root_guard=guard)
        self.indexes: dict[str, tuple[bytes | None, str | None, VersionCatalog]] = {}
        self.trees: dict[str, str] = {}
        self.read_bytes_count = 0
        self.read_count = 0
        self.tree_count = 0
        self.reader = ImmutableVersionReader(
            self._reader_bytes,
            current_index_tail=lambda package_id: self.indexes[package_id][1],
            read_skill_tree_fingerprint=self.tree if owner.read_skill_tree_fingerprint is not None else None,
        )

    def read_bytes(self, relative: str, limit: int) -> bytes | None:
        self.read_count += 1
        remaining = MAX_REQUEST_READ_BYTES - self.read_bytes_count
        if self.read_count > MAX_REQUEST_READS or remaining <= 0:
            raise AIEmployeeContractError("storage_record_invalid", "岗位目录核验超过本次有界读取范围。")
        raw = self.files.read(relative, min(limit, remaining))
        if raw is not None:
            self.read_bytes_count += len(raw)
        return raw

    def anchor(self, package_id: str) -> str | None:
        if self.owner.current_index_tail is None:
            return None
        try:
            value = self.owner.current_index_tail(package_id)
        except Exception:
            raise AIEmployeeContractError("publication_index_unverified", "可信索引尾锚暂不可读取。") from None
        if value is not None and (type(value) is not str or not _SHA.fullmatch(value)):
            raise AIEmployeeContractError("publication_index_invalid", "可信索引尾锚格式无效。")
        return value

    def index(self, package_id: str) -> VersionCatalog:
        if package_id not in self.indexes:
            raw = self.read_bytes(f"employee_packages/{package_id}/versions.jsonl", MAX_LEDGER_BYTES)
            anchor = self.anchor(package_id)
            try:
                catalog = VersionCatalog.from_index(package_id, raw, expected_tail_sha256=anchor)
            except AIEmployeeContractError as exc:
                raise AIEmployeeContractError("publication_index_invalid", "发布索引形状、哈希链或可信尾锚无法核实。") from exc
            self.indexes[package_id] = raw, anchor, catalog
        return self.indexes[package_id][2]

    def _reader_bytes(self, relative: str, limit: int) -> bytes | None:
        # The reader must select from exactly the index already checked for this
        # request. Its internal catalogue reload cannot splice in a newer index.
        parts = relative.split("/")
        if len(parts) == 3 and parts[0] == "employee_packages" and parts[2] == "versions.jsonl":
            self.index(parts[1])
            return self.indexes[parts[1]][0]
        return self.read_bytes(relative, limit)

    def tree(self, relative: str) -> str:
        self.tree_count += 1
        if self.tree_count > MAX_REQUEST_TREE_CHECKS:
            raise AIEmployeeContractError("immutable_skill_tree_unverified", "技能树核验超过本次数量范围。")
        callback = self.owner.read_skill_tree_fingerprint
        with self.files.workspace():
            try:
                actual = callback(relative) if callback is not None else None
            except Exception:
                raise AIEmployeeContractError("immutable_skill_tree_unverified", "技能整树依据暂不可核实。") from None
        if type(actual) is not str or not _SHA.fullmatch(actual):
            raise AIEmployeeContractError("immutable_skill_tree_unverified", "技能整树依据不是规范指纹。")
        before = self.trees.setdefault(relative, actual)
        if before != actual:
            _changed()
        return actual

    def view(self, package_id: str, *, version: str | None = None,
             fingerprint: str | None = None, purpose: str = "detail") -> dict[str, Any]:
        return self.view_with_files(package_id, version=version, fingerprint=fingerprint, purpose=purpose)[0]

    def view_with_files(self, package_id: str, *, version: str | None = None,
                        fingerprint: str | None = None, purpose: str = "detail") -> tuple[dict[str, Any], dict[str, bytes]]:
        """The verified view plus the immutable files it was hashed from.

        The files are the same bytes ``package_fingerprint`` covered; callers
        must not re-read the directory by path.
        """
        catalog = self.index(package_id)
        raw, anchor, _ = self.indexes[package_id]
        view, files = self.reader._snapshot(package_id, version=version, fingerprint=fingerprint, purpose=purpose)
        view["revision"] = _json_digest({
            "schema_version": 1, "package_id": package_id,
            "index_sha256": _digest(raw) if raw is not None else None,
            "index_tail_sha256": catalog.tail_sha256, "independent_tail_sha256": anchor,
            "manifest_sha256": _json_digest(view["package"]),
            "publication": view["publication"], "fingerprint": view["fingerprint"],
        })
        view["status"] = view["publication"]["status"]
        view["source"] = "published"
        view["allowed_actions"] = list(_READ_ACTIONS)
        return view, files

    def verify(self) -> None:
        for relative in sorted(self.trees):
            self.tree(relative)
        for package_id, (raw, anchor, _catalog) in sorted(self.indexes.items()):
            latest = self.read_bytes(f"employee_packages/{package_id}/versions.jsonl", MAX_LEDGER_BYTES)
            if latest != raw or self.anchor(package_id) != anchor:
                _changed()

    def indexed_revision_inputs(self) -> list[dict[str, Any]]:
        # Draft-only directory names and bytes never enter the public response
        # or its revision; their missing index is checked internally nonetheless.
        return [{"package_id": package_id, "index_sha256": _digest(raw),
                 "independent_tail_sha256": anchor}
                for package_id, (raw, anchor, _catalog) in sorted(self.indexes.items()) if raw is not None]


class PublishedEmployeeCatalog:
    """DI provider for EmployeeActionDispatcher, and pinned instance reads.

    Callbacks must originate in trusted server state. A tree callback must use
    the established whole-tree algorithm with native contained regular-file
    reads, be bounded, and never install skills. No callback is sourced from a
    request, and there is no synthetic tree/tail fallback in this adapter.
    """
    def __init__(self, root: Path, *, current_index_tail: Callable[[str], str | None] | None = None,
                 read_skill_tree_fingerprint: Callable[[str], str] | None = None) -> None:
        if any(callback is not None and not callable(callback)
               for callback in (current_index_tail, read_skill_tree_fingerprint)):
            raise AIEmployeeContractError("request_invalid", "目录读取回调必须由服务端显式提供。")
        self.root = Path(os.path.abspath(root))
        self.current_index_tail = current_index_tail
        self.read_skill_tree_fingerprint = read_skill_tree_fingerprint

    @contextmanager
    def _request(self) -> Iterator[_ReadSnapshot | None]:
        files = AIEmployeeSafeFiles(self.root)
        stack = ExitStack()
        try:
            try:
                workspace = stack.enter_context(files.workspace())
            except FileNotFoundError:
                yield None
                # Verify observed root absence without creating or repairing it.
                try:
                    with files.workspace():
                        _changed()
                except FileNotFoundError:
                    return
                return
            with stack:
                yield _ReadSnapshot(self, workspace)
        except WorkspaceBoundaryError as exc:
            raise AIEmployeeContractError("storage_path_invalid", "岗位目录链接或根换位被拒绝。") from exc
        except OSError as exc:
            raise AIEmployeeContractError("storage_record_invalid", "岗位目录暂不能安全读取。") from exc
        finally:
            stack.close()

    @staticmethod
    def _list_result(packages: list[dict[str, Any]], indexes: list[dict[str, Any]]) -> dict[str, Any]:
        result = {
            "packages": packages, "revision": _json_digest({"schema_version": 1,
                "catalog_scope": "visible_index_snapshot", "indexes": indexes,
                "selected_revisions": [view["revision"] for view in packages]}),
            "catalog_scope": "visible_index_snapshot", "is_current_verified": False,
            "membership_current_verified": False, "new_adoption_allowed": False,
            "effects_produced": False, "allowed_actions": list(_READ_ACTIONS), "problems": [],
        }
        canonical_json(result)
        return result

    @staticmethod
    def _package_entries(snapshot: _ReadSnapshot) -> list[dict[str, str]]:
        # FileStore owns precisely these three direct metadata names. SafeFiles
        # has already refused links/nonregular entries before this type check;
        # neither receipt bodies nor lock/journal bytes are read or displayed.
        entries = snapshot.files.list_entries("employee_packages", limit=MAX_PACKAGES + len(_STORE_ENTRIES))
        packages: list[dict[str, str]] = []
        for entry in entries:
            expected = _STORE_ENTRIES.get(entry["name"])
            if expected is not None:
                if entry["kind"] != expected:
                    raise AIEmployeeContractError("storage_record_invalid", "岗位存储内部条目类型无法核实。")
                continue
            if entry["kind"] != "directory" or not _PACKAGE.fullmatch(entry["name"]):
                raise AIEmployeeContractError("storage_record_invalid", "岗位目录含无法核实的直接条目。")
            packages.append(entry)
            if len(packages) > MAX_PACKAGES:
                raise AIEmployeeContractError("storage_record_invalid", "岗位目录超过本次包数量范围。")
        return packages

    def list(self) -> dict[str, Any]:
        with self._request() as snapshot:
            if snapshot is None:
                return self._list_result([], [])
            entries = self._package_entries(snapshot)
            packages: list[dict[str, Any]] = []
            retained_bytes = 0
            for entry in entries:
                package_id = entry["name"]
                catalog = snapshot.index(package_id)
                # All-retired and draft-only catalogues have no default published
                # selection. Malformed indexes above always fail, never skip.
                if any(row["status"] == "published" for row in catalog.list()):
                    view = snapshot.view(package_id)
                    retained_bytes += len(canonical_json(view))
                    if retained_bytes > MAX_LIST_VIEW_BYTES:
                        raise AIEmployeeContractError("storage_record_invalid", "岗位目录展示超过本次有界读取范围。")
                    packages.append(view)
            if self._package_entries(snapshot) != entries:
                _changed()
            snapshot.verify()
            return self._list_result(packages, snapshot.indexed_revision_inputs())

    def read(self, package_id: str, *, version: str | None = None,
             fingerprint: str | None = None, purpose: str = "detail") -> dict[str, Any]:
        package_id = _package_id(package_id)
        with self._request() as snapshot:
            if snapshot is None:
                raise AIEmployeeContractError("version_not_available", "指定岗位没有可见发布版。")
            view = snapshot.view(package_id, version=version, fingerprint=fingerprint, purpose=purpose)
            snapshot.verify()
            canonical_json(view)
            return view

    def read_with_files(self, package_id: str, *, version: str | None = None,
                        fingerprint: str | None = None, purpose: str = "pinned") -> tuple[dict[str, Any], dict[str, bytes]]:
        """Like ``read``, plus the immutable package files the view was hashed from."""
        package_id = _package_id(package_id)
        with self._request() as snapshot:
            if snapshot is None:
                raise AIEmployeeContractError("version_not_available", "指定岗位没有可见发布版。")
            view, files = snapshot.view_with_files(package_id, version=version, fingerprint=fingerprint, purpose=purpose)
            snapshot.verify()
            canonical_json(view)
            return view, files

    def detail(self, package_id: str, *, version: str | None = None) -> dict[str, Any]:
        return self.read(package_id, version=version)

    def package_loader(self, package_id: str, version: str, fingerprint: str) -> dict[str, Any]:
        """Existing instance DI signature; never replace its frozen fingerprint."""
        view = self.read(package_id, version=version, fingerprint=fingerprint, purpose="pinned")
        return {"manifest": view["package"], "fingerprint": view["fingerprint"],
                "status": view["status"], "new_adoption_allowed": view["new_adoption_allowed"],
                "is_current_verified": view["is_current_verified"], "revision": view["revision"],
                "effects_produced": False}
