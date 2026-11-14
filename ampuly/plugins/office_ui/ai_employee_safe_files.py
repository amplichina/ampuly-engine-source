"""Read-only, bounded AE asset I/O through the existing pinned workspace API.

This is not an authorization layer. Callers choose an authenticated scope and
validate object families before using it. Missing reads never create a root,
lock or recovery record. The unsafe lexical workspace downgrade is refused.
The few backend-private operations below are confined to this adapter, because
Windows directory enumeration is not exposed by Python's directory-fd API.
"""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from ampuly.layer4_tools.workspace_fs import (
    LexicalWorkspace,
    SecureWorkspace,
    WorkspaceBoundaryError,
    WorkspacePath,
    _secure_primitives_available,
)
from .ai_employee_contract import AIEmployeeContractError

MAX_READ_BYTES = 20 * 1024 * 1024
MAX_LIST_ENTRIES = 10_000
_DOS_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{x}" for x in "123456789¹²³"), *(f"LPT{x}" for x in "123456789¹²³")}


def validate_relative(relative: str) -> tuple[str, ...]:
    """Only canonical relative names in the two AE asset families are accepted."""
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise AIEmployeeContractError("storage_path_invalid", "岗位资产路径无效。")
    parts = PurePosixPath(relative).parts
    if (not parts or parts[0] not in {"employee_packages", "projects"}
            or relative != "/".join(parts)
            or any(p in {"", ".", ".."} or p.endswith((" ", "."))
                   or p.split(".", 1)[0].rstrip(" .").upper() in _DOS_NAMES
                   or any(ord(c) < 32 or ord(c) == 127 for c in p) for p in parts)):
        raise AIEmployeeContractError("storage_path_invalid", "岗位资产路径无效。")
    return parts


def _identity(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    return int(info.st_dev), int(info.st_ino)


class AIEmployeeSafeFiles:
    def __init__(self, root: Path, *, root_guard: Callable[[Any], None] | None = None) -> None:
        # Never resolve a link and then trust its destination.
        self.root = Path(os.path.abspath(root))
        # Optional writer-lease identity fence, not client-supplied authority.
        self.root_guard = root_guard

    @contextmanager
    def workspace(self) -> Iterator[Any]:
        """One pinned root for an operation; no creation and no downgrade."""
        if not _secure_primitives_available():
            raise WorkspaceBoundaryError("Native handle-relative storage is unavailable")
        capability = SecureWorkspace.for_roots(str(self.root), str(self.root))
        if isinstance(capability, LexicalWorkspace):
            raise WorkspaceBoundaryError("Lexical storage is not supported")
        with capability as workspace:
            if self.root_guard is not None:
                self.root_guard(workspace)
            yield workspace
            if self.root_guard is not None:
                self.root_guard(workspace)
            # A pinned handle must still be the root named by the configured
            # installation. Reopening is verification only, never repair.
            try:
                with SecureWorkspace.for_roots(str(self.root), str(self.root)) as current:
                    if os.name == "nt":
                        same = workspace._identity(workspace.root_handle) == current._identity(current.root_handle)
                    else:
                        same = _identity(workspace.root_fd) == _identity(current.root_fd)
                    if not same:
                        raise WorkspaceBoundaryError("Asset root changed during operation")
            except FileNotFoundError as exc:
                raise WorkspaceBoundaryError("Asset root disappeared during operation") from exc

    @staticmethod
    def target(workspace: Any, relative: str) -> WorkspacePath:
        validate_relative(relative)
        return workspace.resolve(relative, use_output_root=False)

    def read(self, relative: str, limit: int) -> bytes | None:
        validate_relative(relative)
        if type(limit) is not int or not 0 < limit <= MAX_READ_BYTES:
            raise AIEmployeeContractError("storage_record_invalid", "岗位记录大小范围无效。")
        try:
            with self.workspace() as workspace:
                target = self.target(workspace, relative)
                fd = workspace.open_read(target)
                try:
                    before = os.fstat(fd)
                    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                        raise AIEmployeeContractError("storage_record_invalid", "岗位记录超过范围或不是普通文件。")
                    chunks: list[bytes] = []
                    remaining = limit + 1
                    while remaining:
                        chunk = os.read(fd, min(remaining, 64 * 1024))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    raw = b"".join(chunks)
                    after = os.fstat(fd)
                    if (len(raw) > limit or before.st_size != after.st_size
                            or before.st_mtime_ns != after.st_mtime_ns):
                        raise AIEmployeeContractError("storage_record_invalid", "岗位记录过大或读取过程中发生变化。")
                    try:
                        current = workspace.open_read(target)
                    except FileNotFoundError as exc:
                        raise WorkspaceBoundaryError("Asset name disappeared during read") from exc
                    try:
                        if _identity(current) != _identity(fd):
                            raise WorkspaceBoundaryError("Asset name changed during read")
                    finally:
                        os.close(current)
                    return raw
                finally:
                    os.close(fd)
        except FileNotFoundError:
            return None
        except WorkspaceBoundaryError as exc:
            raise AIEmployeeContractError("storage_path_invalid", "岗位资产链接或路径换位被拒绝。") from exc

    def list_entries(self, relative: str, *, limit: int = 2001) -> list[dict[str, str]]:
        """List immediate ordinary entries only; never recurse or omit links."""
        validate_relative(relative)
        if type(limit) is not int or not 0 < limit <= MAX_LIST_ENTRIES:
            raise AIEmployeeContractError("storage_record_invalid", "岗位清单数量范围无效。")
        try:
            with self.workspace() as workspace:
                target = self.target(workspace, relative)
                rows = self._list_windows(workspace, target, limit) if os.name == "nt" else self._list_posix(workspace, target, limit)
                return sorted(rows, key=lambda row: row["name"])
        except FileNotFoundError:
            return []
        except WorkspaceBoundaryError as exc:
            raise AIEmployeeContractError("storage_path_invalid", "岗位资产清单包含链接或路径换位。") from exc

    @staticmethod
    def _append(rows: list[dict[str, str]], name: str, directory: bool, limit: int) -> None:
        if len(rows) >= limit:
            raise AIEmployeeContractError("storage_record_invalid", "岗位资产清单超过数量范围。")
        rows.append({"name": name, "kind": "directory" if directory else "file"})

    @classmethod
    def _list_posix(cls, workspace: Any, target: WorkspacePath, limit: int) -> list[dict[str, str]]:
        fd = workspace.open_directory(target)
        try:
            rows: list[dict[str, str]] = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    directory = stat.S_ISDIR(info.st_mode)
                    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                        raise WorkspaceBoundaryError("Unsupported asset entry")
                    child = WorkspacePath((*target.parts, entry.name), target.display_path / entry.name)
                    checked = workspace.open_directory(child) if directory else workspace.open_read(child)
                    try:
                        if _identity(checked) != (int(info.st_dev), int(info.st_ino)):
                            raise WorkspaceBoundaryError("Asset entry changed during listing")
                    finally:
                        os.close(checked)
                    cls._append(rows, entry.name, directory, limit)
            current = workspace.open_directory(target)
            try:
                if _identity(current) != _identity(fd):
                    raise WorkspaceBoundaryError("Asset directory changed during listing")
            finally:
                os.close(current)
            return rows
        except FileNotFoundError as exc:
            raise WorkspaceBoundaryError("Asset entry disappeared during listing") from exc
        finally:
            os.close(fd)

    @classmethod
    def _list_windows(cls, workspace: Any, target: WorkspacePath, limit: int) -> list[dict[str, str]]:
        from ampuly.layer4_tools.workspace_fs_windows import _FILE_ATTRIBUTE_DIRECTORY, _FILE_ATTRIBUTE_REPARSE_POINT

        handle = workspace._open_directory_handle(target)
        try:
            expected = workspace._identity(handle)
            rows: list[dict[str, str]] = []
            for name, attributes, _size in workspace._directory_rows(handle):
                if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                    raise WorkspaceBoundaryError("Reparse asset entry")
                directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
                child = WorkspacePath((*target.parts, name), target.display_path / name)
                checked = workspace._open_directory_handle(child) if directory else workspace._open_file_handle(handle, name, flags=os.O_RDONLY)
                workspace._close_handle(checked)
                cls._append(rows, name, directory, limit)
            current = workspace._open_directory_handle(target)
            try:
                if workspace._identity(current) != expected:
                    raise WorkspaceBoundaryError("Asset directory changed during listing")
            finally:
                workspace._close_handle(current)
            return rows
        except FileNotFoundError as exc:
            raise WorkspaceBoundaryError("Asset entry disappeared during listing") from exc
        finally:
            workspace._close_handle(handle)
