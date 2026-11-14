"""Small native-only write/lock adapter for authenticated AE storage scopes.

The caller owns authorization, object whitelists and transaction recovery. All
opens, directory creation and publication are relative to pinned native parent
handles. A successful write retains a receipt until the named result is checked;
an uncertain publication is never rolled back or retried here. Windows cleanup
deletes the original temporary object by its still-open handle. Portable POSIX
has no unlink-by-fd: on failure its unpublished temporary may remain rather than
deleting a name which another process could have substituted.
"""
from __future__ import annotations

import errno
import os
import secrets
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

from ampuly.core.file_lock import lock_file_descriptor, unlock_file_descriptor
from ampuly.layer4_tools.workspace_fs import (
    LexicalWorkspace,
    SecureWorkspace,
    WorkspaceBoundaryError,
    WorkspacePath,
    _secure_primitives_available,
)
from .ai_employee_contract import AIEmployeeContractError
from .ai_employee_safe_files import AIEmployeeSafeFiles, validate_relative

MAX_WRITE_BYTES = 20 * 1024 * 1024
RootGuard = Callable[[Any], None]


def _unknown() -> AIEmployeeContractError:
    return AIEmployeeContractError("outcome_unknown", "岗位资产根、锁或保存结果发生换位，请核实原请求。")


def _root_identity(workspace: Any) -> tuple[int, int]:
    return _directory_identity(workspace, workspace.root_handle if os.name == "nt" else workspace.root_fd)


def _directory_identity(workspace: Any, handle: int) -> tuple[int, int]:
    if os.name == "nt":
        return workspace._identity(handle)
    info = os.fstat(handle)
    return int(info.st_dev), int(info.st_ino)


def _file_identity(workspace: Any, fd: int) -> tuple[int, int]:
    if os.name == "nt":
        return workspace._identity(workspace._handle_for_fd(fd))
    info = os.fstat(fd)
    return int(info.st_dev), int(info.st_ino)


@contextmanager
def _root_chain(root: Path, *, create: bool) -> Iterator[tuple[Any, int]]:
    """Open only the volume anchor absolutely; walk/create every other part."""
    if not _secure_primitives_available():
        raise WorkspaceBoundaryError("Native handle-relative storage is unavailable")
    if not root.is_absolute() or len(root.parts) < 2:
        raise WorkspaceBoundaryError("A non-volume absolute storage scope is required")
    anchor = Path(root.anchor)
    capability = SecureWorkspace.for_roots(str(anchor), str(anchor))
    if isinstance(capability, LexicalWorkspace):
        raise WorkspaceBoundaryError("Lexical storage is not supported")
    # The final component is a placeholder, never opened or created as a file.
    probe = WorkspacePath((*root.parts[1:], "_ae_root_probe"), root / "_ae_root_probe")
    with capability as walker:
        with walker._open_parent(probe, create_dirs=create) as (root_handle, _name):
            yield walker, root_handle


def _assert_root_named(root: Path, workspace: Any) -> None:
    try:
        with _root_chain(root, create=False) as (walker, current):
            if _directory_identity(walker, current) != _root_identity(workspace):
                raise _unknown()
    except (FileNotFoundError, WorkspaceBoundaryError) as exc:
        raise _unknown() from exc


@contextmanager
def _writable_workspace(root: Path, root_guard: RootGuard | None = None) -> Iterator[Any]:
    if root_guard is not None and not callable(root_guard):
        raise AIEmployeeContractError("storage_record_invalid", "岗位根守卫无效。")
    files = AIEmployeeSafeFiles(root)
    try:
        # With a live lock guard, even root bootstrap is prohibited. The locked
        # root must already exist and must be checked before any child creation.
        with _root_chain(files.root, create=root_guard is None) as (walker, pinned_root):
            with files.workspace() as workspace:
                if _directory_identity(walker, pinned_root) != _root_identity(workspace):
                    raise _unknown()
                if root_guard is not None:
                    root_guard(workspace)
                yield workspace
                _assert_root_named(files.root, workspace)
                if root_guard is not None:
                    root_guard(workspace)
    except FileNotFoundError as exc:
        if root_guard is not None:
            raise _unknown() from exc
        raise


def _open_at(workspace: Any, parent: int, name: str, flags: int, *, temporary: bool = False) -> int:
    if os.name == "nt":
        handle = workspace._open_file_handle(parent, name, flags=flags, delete_access=temporary)
        return workspace._fd_for_handle(handle, flags)
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_NONBLOCK", 0), mode=0o600, dir_fd=parent)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise WorkspaceBoundaryError("Linked or swapped asset file") from exc
        raise
    try:
        workspace._regular_file(fd, workspace.root / name)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _assert_parent_named(workspace: Any, target: WorkspacePath, parent: int) -> None:
    try:
        with workspace._open_parent(target) as (current, _name):
            if _directory_identity(workspace, current) != _directory_identity(workspace, parent):
                raise _unknown()
        if os.name == "nt":
            final = Path(workspace._final_path(parent))
            root_final = Path(workspace._final_path(workspace.root_handle))
            if final.relative_to(root_final).parts != target.parts[:-1]:
                raise WorkspaceBoundaryError("Asset parent spelling or location changed")
        else:
            workspace._require_exact_spelling(WorkspacePath(target.parts[:-1], target.display_path.parent))
    except (FileNotFoundError, ValueError) as exc:
        raise _unknown() from exc


def _assert_named_file(workspace: Any, target: WorkspacePath, parent: int, fd: int) -> None:
    _assert_parent_named(workspace, target, parent)
    try:
        named = workspace.open_read(target)
    except FileNotFoundError as exc:
        raise _unknown() from exc
    try:
        if _file_identity(workspace, named) != _file_identity(workspace, fd):
            raise _unknown()
    finally:
        os.close(named)


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    offset = 0
    while offset < len(view):
        count = os.write(fd, view[offset:offset + 64 * 1024])
        if count <= 0:
            raise OSError("Short write while updating an AE asset")
        offset += count


def _atomic_bytes(workspace: Any, target: WorkspacePath, parent: int, name: str,
                  raw: bytes, root_guard: RootGuard | None) -> None:
    temp_fd: int | None = None
    publish_attempted = False
    try:
        # Reject existing links/hardlinks/aliases before touching any temporary.
        try:
            previous = _open_at(workspace, parent, name, os.O_RDONLY)
        except FileNotFoundError:
            previous = None
        if previous is not None:
            try:
                _assert_named_file(workspace, target, parent, previous)
            finally:
                os.close(previous)
        for _ in range(32):
            temp_name = f".ampuly-write-{secrets.token_hex(12)}.tmp"
            try:
                temp_fd = _open_at(workspace, parent, temp_name,
                                   os.O_RDWR | os.O_CREAT | os.O_EXCL, temporary=True)
                break
            except FileExistsError:
                continue
        if temp_fd is None:
            raise FileExistsError("Unable to allocate a unique AE temporary file")
        _write_all(temp_fd, raw)
        os.fsync(temp_fd)
        _assert_parent_named(workspace, target, parent)
        if root_guard is not None:
            root_guard(workspace)
        # POSIX rename has a source name, so check that the unique temporary is
        # still our object. The receipt check below rejects any later swap; this
        # does not claim a hostile same-user namespace has compare-and-swap.
        source = _open_at(workspace, parent, temp_name, os.O_RDONLY)
        try:
            if _file_identity(workspace, source) != _file_identity(workspace, temp_fd):
                raise _unknown()
        finally:
            os.close(source)
        publish_attempted = True
        if os.name == "nt":
            workspace._set_rename(workspace._handle_for_fd(temp_fd), parent, name)
        else:
            os.rename(temp_name, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        os.fsync(temp_fd)
        _assert_named_file(workspace, target, parent, temp_fd)
        _assert_root_named(workspace.root, workspace)
        if root_guard is not None:
            root_guard(workspace)
    except (OSError, AIEmployeeContractError) as exc:
        if publish_attempted:
            raise _unknown() from exc
        raise
    finally:
        if temp_fd is not None:
            if not publish_attempted and os.name == "nt":
                # Delete exactly our held object, never reopen a guessed name.
                with suppress(OSError):
                    workspace._set_delete(workspace._handle_for_fd(temp_fd))
            os.close(temp_fd)


def atomic_write(root: Path, relative: str, raw: bytes, *, root_guard: RootGuard | None = None) -> None:
    """Publish exact bounded bytes; unknown outcomes require caller reconciliation."""
    if len(validate_relative(relative)) < 2:
        raise AIEmployeeContractError("storage_path_invalid", "岗位保存需要文件路径。")
    if type(raw) is not bytes or len(raw) > MAX_WRITE_BYTES:
        raise AIEmployeeContractError("storage_record_invalid", "岗位保存正文类型或大小无效。")
    published = False
    try:
        with _writable_workspace(root, root_guard) as workspace:
            target = AIEmployeeSafeFiles.target(workspace, relative)
            with workspace._open_parent(target, create_dirs=True) as (parent, name):
                _assert_parent_named(workspace, target, parent)
                _atomic_bytes(workspace, target, parent, name, raw, root_guard)
                published = True
    except WorkspaceBoundaryError as exc:
        if published:
            raise _unknown() from exc
        raise AIEmployeeContractError("storage_path_invalid", "岗位保存路径包含链接、别名或换位。") from exc


@contextmanager
def open_locked(root: Path, relative: str) -> Iterator[BinaryIO]:
    """Nonblocking native lock; stream.ae_assert_root guards subsequent operations.

    ae_assert_root(workspace=None) is valid only inside this context. With an
    operation's already-opened workspace it checks that root before its I/O,
    then independently verifies the configured root chain and lock identity.
    No existing payload is truncated or padded merely to obtain the lock.
    """
    if len(validate_relative(relative)) < 2:
        raise AIEmployeeContractError("storage_path_invalid", "岗位锁需要文件路径。")
    fd: int | None = None
    locked = False
    active = False
    stream: BinaryIO | None = None
    try:
        with _writable_workspace(root) as workspace:
            target = AIEmployeeSafeFiles.target(workspace, relative)
            with workspace._open_parent(target, create_dirs=True) as (parent, name):
                _assert_parent_named(workspace, target, parent)
                fd = _open_at(workspace, parent, name, os.O_RDWR | os.O_CREAT)
                try:
                    lock_file_descriptor(fd, blocking=False)
                except OSError as exc:
                    if isinstance(exc, BlockingIOError) or exc.errno in {errno.EACCES, errno.EAGAIN} or getattr(exc, "winerror", None) in {33, 158}:
                        raise AIEmployeeContractError("storage_busy", "岗位资产正在被另一个保存操作占用。") from exc
                    raise
                locked = active = True
                expected_root = _root_identity(workspace)

                def guard(operation_workspace: Any = None) -> None:
                    if not active:
                        raise _unknown()
                    if operation_workspace is not None and _root_identity(operation_workspace) != expected_root:
                        raise _unknown()
                    _assert_root_named(workspace.root, workspace)
                    try:
                        _assert_named_file(workspace, target, parent, fd)
                    except WorkspaceBoundaryError as exc:
                        raise _unknown() from exc

                guard()
                stream = os.fdopen(fd, "r+b", closefd=False)
                stream.ae_assert_root = guard  # type: ignore[attr-defined]
                try:
                    yield stream
                finally:
                    try:
                        guard()
                    finally:
                        active = False
                        try:
                            stream.close()
                        finally:
                            stream = None
                            try:
                                if locked:
                                    locked = False
                                    unlock_file_descriptor(fd)
                            finally:
                                closing_fd, fd = fd, None
                                os.close(closing_fd)
    except WorkspaceBoundaryError as exc:
        raise AIEmployeeContractError("storage_path_invalid", "岗位锁路径包含链接、别名或换位。") from exc
    finally:
        active = False
        if stream is not None:
            stream.close()
        if fd is not None:
            try:
                if locked:
                    unlock_file_descriptor(fd)
            finally:
                os.close(fd)
