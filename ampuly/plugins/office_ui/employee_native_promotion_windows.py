"""Bounded Windows-only, no-replace directory promotion primitive.

This is NOT a package writer, tree/content verifier, authorization boundary,
durable publication receipt, or a cross-process effect witness. The host must
call creation only AFTER its durable one-shot publication marker. Existing
roots, .publication/<request_key> parents and .versions parents are required.
No directory is adopted, repaired, recursively inspected or deleted. Explicit
population mode accepts a complete bounded frozen package in this invocation;
it is not a host permission or an independent publication observation.

The validated named root is pinned without SHARE_DELETE and its identity is
compared to the still-open original handle; every ancestor
and the freshly FILE_CREATE'd stage is also opened without SHARE_DELETE. A
sharing conflict fails closed. The stage DELETE handle must be closed after a
successful rename before a same-policy target read-open can succeed. Replacement
in that short interval yields unknown, never a retry. A verified target handle
is retained until context exit. This receipt describes only this call's native
directory identity observation, not content or permission.

Official native contracts:
https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information
https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-reopenfile
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from threading import Lock
from typing import Any, Iterator

from .ai_employee_contract import AIEmployeeContractError
from .ai_employee_safe_files import validate_relative

MAX_ACTIVE_STAGES = 8
MAX_RELATIVE_BYTES = 256
MAX_COMPONENTS = 5
_CHUNK_BYTES = 64 * 1024
_DIRECTORY_ROWS = 256
_DIRECTORY_QUERIES = 259  # Bounded package entries, dot rows, terminal query.
_FINAL_PATH_CHARS = 32768
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_COLLISION = 0xC0000035
_TOKEN = object()
_REGISTRY_LOCK = Lock()
_ACTIVE: dict[ExclusiveStage, _State] = {}


def _error(code: str) -> AIEmployeeContractError:
    messages = {
        "promotion_path_invalid": "目录推广身份或路径格式无效。",
        "promotion_native_unavailable": "原生目录推广能力不可用。",
        "promotion_stage_limit": "本进程目录推广句柄已达到上限。",
        "promotion_stage_exists": "暂存目录已存在，不能认领或覆盖。",
        "promotion_stage_unavailable": "目录推广所需的现有根或父目录未确认。",
        "promotion_creation_unknown": "暂存创建未确认；保留原请求，不得重新创建。",
        "promotion_witness_invalid": "没有本叶仍存活的原始暂存见证。",
        "promotion_lookup_only": "该目录推广已尝试；仅核对原请求。",
        "promotion_target_exists": "固定版本目录已存在，不能覆盖或认领。",
        "promotion_outcome_unknown": "目录推广未确认；仅核对原请求，不得重派。",
        "promotion_population_mode_invalid": "没有开启本次暂存填充能力。",
        "promotion_population_plan_invalid": "冻结填充内容不符合完整有界约束。",
        "promotion_population_nested_unsupported": "本次填充路径超过四层边界。",
        "promotion_population_lookup_only": "本次暂存填充已尝试，不得再次填充。",
        "promotion_population_incomplete": "暂存内容未完整确认，不能推广。",
        "promotion_population_native_unavailable": "原生暂存填充能力不可用。",
        "promotion_population_unknown": "暂存填充未确认；保留原请求及原部分对象。",
    }
    return AIEmployeeContractError(code, messages[code])


def _identifier(value: Any, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        raise _error("promotion_path_invalid")
    return value


def _parts(package_id: str, request_key: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    package_id = _identifier(package_id, _PACKAGE)
    request_key = _identifier(request_key, _SHA)
    source = f"employee_packages/{package_id}/.publication/{request_key}/stage"
    parent = f"employee_packages/{package_id}/.versions"
    for relative in (source, parent):
        if len(relative.encode("utf-8")) > MAX_RELATIVE_BYTES:
            raise _error("promotion_path_invalid")
        parts = validate_relative(relative)
        if len(parts) > MAX_COMPONENTS:
            raise _error("promotion_path_invalid")
    return validate_relative(source), validate_relative(parent)


def _load_native() -> Any:
    if os.name != "nt":
        raise _error("promotion_native_unavailable")
    from ampuly.layer4_tools import workspace_fs_windows as native

    names = ("_FILE_RENAME_INFO", "_IO_STATUS_BLOCK", "wintypes", "_ntdll", "_kernel32")
    if (not native.windows_backend_available()
            or any(getattr(native, name, None) is None for name in names)
            or not callable(getattr(native._kernel32, "CreateFileW", None))):
        raise _error("promotion_native_unavailable")
    return native


class ExclusiveStage:
    """Opaque, context-bound input; ordinary receipt/echo objects are rejected."""
    __slots__ = ()

    def __new__(cls, token: object = None):
        if token is not _TOKEN or cls is not ExclusiveStage:
            raise TypeError("ExclusiveStage is created only by the native primitive")
        return super().__new__(cls)

    def __repr__(self) -> str:
        return "<ExclusiveStage call-local>"


@dataclass(frozen=True, slots=True)
class PromotionReceipt:
    """Local observation only; this type is never accepted as promotion input."""
    package_id: str
    request_key: str
    fingerprint: str
    observation_scope: str = field(default="directory_identity_this_call", init=False)
    _identity: tuple[int, int] = field(default=(0, 0), repr=False)


@dataclass
class _State:
    native: Any
    workspace: Any
    package_id: str
    request_key: str
    source_parts: tuple[str, ...]
    target_parent_parts: tuple[str, ...]
    handles: list[int] = field(default_factory=list)
    root: int | None = None
    source_chain: list[int] = field(default_factory=list)
    target_chain: list[int] = field(default_factory=list)
    stage: int | None = None
    created_identity: tuple[int, int] | None = None
    attempted: bool = False
    for_population: bool = False
    population_attempted: bool = False
    population_complete: bool = False
    population_fingerprint: str | None = None
    population_files: tuple[_PopulatedFile, ...] = ()
    population_directories: dict[str, tuple[int, tuple[int, int]]] = field(default_factory=dict)
    population_manifest_raw: bytes = b""


@dataclass(frozen=True, repr=False)
class _PopulatedFile:
    name: str
    handle: int
    identity: tuple[int, int]
    raw: bytes
    sha256: str


def _close(state: _State, handle: int) -> None:
    # An unconfirmed close is not retried: an integer handle could be reused.
    state.handles.remove(handle)
    if not state.native._kernel32.CloseHandle(state.native.wintypes.HANDLE(handle)):
        raise _error("promotion_outcome_unknown")


def _check_directory(state: _State, handle: int) -> tuple[int, int]:
    # Reuse the native workspace's actual attribute/reparse/type checks.
    state.workspace._validate_handle(handle, state.workspace.root, directory=True)
    identity = state.workspace._identity(handle)
    if (type(identity) is not tuple or len(identity) != 2
            or any(type(part) is not int or part < 0 for part in identity)):
        raise _error("promotion_stage_unavailable")
    return identity


def _tight_root(state: _State) -> int:
    native = state.native
    original = state.workspace.root_handle  # Entered native workspace is mandatory.
    original_identity = state.workspace._identity(original)
    # ReOpenFile can reject an otherwise accessible directory on Windows.
    # This is NOT an unchecked path fallback: the original handle remains
    # open, the new pin denies DELETE sharing, and identity/type/reparse
    # checks must agree before any child is opened or any write is attempted.
    result = native._kernel32.CreateFileW(str(state.workspace.root),
        native._FILE_LIST_DIRECTORY | native._FILE_TRAVERSE | native._FILE_READ_ATTRIBUTES | native._SYNCHRONIZE,
        native._FILE_SHARE_READ | native._FILE_SHARE_WRITE, None, native._OPEN_EXISTING,
        native._FILE_FLAG_BACKUP_SEMANTICS | native._FILE_FLAG_OPEN_REPARSE_POINT, None)
    handle = int(result) if result is not None else 0
    if not handle or handle == native._INVALID_HANDLE_VALUE:
        raise _error("promotion_stage_unavailable")
    state.handles.append(handle)
    if _check_directory(state, handle) != original_identity:
        raise _error("promotion_stage_unavailable")
    return handle


def _open_directory(state: _State, parent: int, name: str, *, create: bool = False,
                    delete_access: bool = True, list_access: bool = False) -> int:
    """Only one canonical component; deny sharing DELETE for every owned pin."""
    native = state.native
    native._validate_component(name)
    encoded = name.encode("utf-16-le")
    name_buffer = ctypes.create_unicode_buffer(name)
    name_value = native._UNICODE_STRING(
        Length=len(encoded), MaximumLength=len(encoded) + ctypes.sizeof(native.wintypes.WCHAR),
        Buffer=ctypes.cast(name_buffer, native.wintypes.LPWSTR))
    attributes = native._OBJECT_ATTRIBUTES(
        Length=ctypes.sizeof(native._OBJECT_ATTRIBUTES), RootDirectory=native.wintypes.HANDLE(parent),
        ObjectName=ctypes.pointer(name_value), Attributes=native._OBJ_CASE_INSENSITIVE,
        SecurityDescriptor=None, SecurityQualityOfService=None)
    result = native.wintypes.HANDLE()
    try:
        status = int(native._ntdll.NtCreateFile(
            ctypes.byref(result),
            native._FILE_TRAVERSE | native._FILE_READ_ATTRIBUTES | native._SYNCHRONIZE
            | (native._DELETE if create and delete_access else 0)
            | (native._FILE_LIST_DIRECTORY if list_access or create and state.for_population else 0),
            ctypes.byref(attributes), ctypes.byref(native._IO_STATUS_BLOCK()), None,
            native._FILE_ATTRIBUTE_NORMAL, native._FILE_SHARE_READ | native._FILE_SHARE_WRITE,
            native._FILE_CREATE if create else native._FILE_OPEN,
            native._FILE_DIRECTORY_FILE | native._FILE_OPEN_REPARSE_POINT | native._FILE_SYNCHRONOUS_IO_NONALERT,
            None, 0))
    finally:
        # A native call can fill its output then Python cancellation can arrive
        # before return processing. Retain only that returned operation handle.
        handle = int(result.value) if result.value is not None else 0
        if handle and handle != native._INVALID_HANDLE_VALUE:
            state.handles.append(handle)
    if status != 0:
        if create and status & 0xFFFFFFFF == _COLLISION:
            raise _error("promotion_stage_exists")
        raise _error("promotion_creation_unknown" if create else "promotion_stage_unavailable")
    if not handle or handle == native._INVALID_HANDLE_VALUE:
        raise _error("promotion_creation_unknown" if create else "promotion_stage_unavailable")
    _check_directory(state, handle)
    return handle


def _chain(state: _State, parts: tuple[str, ...]) -> list[int]:
    chain: list[int] = []
    current = state.root
    for name in parts:
        current = _open_directory(state, current, name)
        chain.append(current)  # Keep EVERY ancestor until context exit.
    return chain


def _verify_root_and_parents(state: _State) -> None:
    # Named-root reopen is verification only, not a path-based write fallback.
    current = state.workspace._open_absolute_root(state.workspace.root)
    state.handles.append(current)
    original_error: BaseException | None = None
    try:
        if (state.workspace._identity(current) != _check_directory(state, state.root)
                or state.workspace._identity(state.workspace.root_handle)
                != state.workspace._identity(state.root)):
            raise _error("promotion_stage_unavailable")
    except BaseException as exc:
        original_error = exc
        raise
    finally:
        try:
            _close(state, current)
        except BaseException as cleanup_error:
            if (original_error is None or isinstance(original_error, Exception)
                    and not isinstance(cleanup_error, Exception)):
                raise
    for parts, pinned in ((state.source_parts[:-1], state.source_chain),
                          (state.target_parent_parts, state.target_chain)):
        parent = state.root
        for name, original in zip(parts, pinned, strict=True):
            current = _open_directory(state, parent, name)
            original_error = None
            try:
                if (_check_directory(state, current) != _check_directory(state, original)
                        or state.workspace._identity(current)[0] != state.workspace._identity(state.root)[0]):
                    raise _error("promotion_stage_unavailable")
            except BaseException as exc:
                original_error = exc
                raise
            finally:
                try:
                    _close(state, current)
                except BaseException as cleanup_error:
                    if (original_error is None or isinstance(original_error, Exception)
                            and not isinstance(cleanup_error, Exception)):
                        raise
            parent = original


@contextmanager
def create_exclusive_stage(workspace: Any, *, package_id: str,
                           request_key: str, for_population: bool = False) -> Iterator[ExclusiveStage]:
    """Create ONLY .publication/<request_key>/stage; never create ancestors.

    Enter only after the host's confirmed durable marker. A created directory
    may remain on any later failure/exit, including an unused empty stage. This
    primitive never deletes it and never reclaims it on another invocation.
    """
    if type(for_population) is not bool:
        raise _error("promotion_population_mode_invalid")
    source, parent = _parts(package_id, request_key)
    native = _load_native()
    if type(workspace) is not native.WindowsSecureWorkspace:
        raise _error("promotion_native_unavailable")
    witness = ExclusiveStage(_TOKEN)
    state = _State(native, workspace, package_id, request_key, source, parent)
    state.for_population = for_population
    with _REGISTRY_LOCK:
        if len(_ACTIVE) >= MAX_ACTIVE_STAGES:
            raise _error("promotion_stage_limit")
        _ACTIVE[witness] = state
    created_attempted = False
    body_error: BaseException | None = None
    try:
        try:
            state.root = _tight_root(state)
            state.source_chain = _chain(state, source[:-1])
            state.target_chain = _chain(state, parent)
            _verify_root_and_parents(state)
            created_attempted = True
            state.stage = _open_directory(state, state.source_chain[-1], "stage", create=True)
            state.created_identity = _check_directory(state, state.stage)
            _verify_root_and_parents(state)
        except AIEmployeeContractError as exc:
            if exc.code == "promotion_stage_exists":
                raise
            raise _error("promotion_creation_unknown" if created_attempted else "promotion_stage_unavailable") from None
        except Exception:
            raise _error("promotion_creation_unknown" if created_attempted else "promotion_stage_unavailable") from None
        yield witness
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        with _REGISTRY_LOCK:
            _ACTIVE.pop(witness, None)
        cleanup_error: BaseException | None = None
        for handle in list(reversed(state.handles)):
            try:
                _close(state, handle)
            except BaseException as exc:
                # Consume each handle once and continue this finite owned list,
                # even when cleanup itself is cancelled. Prefer its first
                # cancellation over an ordinary cleanup failure.
                if (cleanup_error is None or isinstance(cleanup_error, Exception)
                        and not isinstance(exc, Exception)):
                    cleanup_error = exc
        if body_error is not None and not isinstance(body_error, Exception):
            pass  # Preserve the original setup/body cancellation already raised.
        elif cleanup_error is not None and not isinstance(cleanup_error, Exception):
            raise cleanup_error
        elif cleanup_error is not None and body_error is None:
            raise _error("promotion_outcome_unknown") from None


def _population_payload(state: _State, plan: Any, snapshot: Any) -> tuple[str, tuple[tuple[str, bytes], ...]]:
    # Lazy, real pure contracts; no reader callback or client-supplied path root.
    from .ai_employee_contract import MAX_FILE_BYTES, MAX_MANIFEST_BYTES, canonical_json, package_fingerprint
    from .employee_package_versions import _decode
    from .employee_publication_authority import FrozenPublication, _plan_data
    from .employee_publication_contents import bounded_publication_paths, publication_content_fingerprint
    from .employee_publication_source_snapshot import (
        PublicationSourceFile, PublicationSourceSnapshot, _source_manifest,
    )

    if type(plan) is not FrozenPublication or type(snapshot) is not PublicationSourceSnapshot:
        raise _error("promotion_population_plan_invalid")
    data = _plan_data(plan)
    if (plan.request.action != "employee_package_publish"
            or type(plan.manifest_raw) is not bytes or len(plan.manifest_raw) > MAX_MANIFEST_BYTES
            or snapshot.request_key != state.request_key or snapshot.request_key != plan.request.key()
            or snapshot.package_id != state.package_id or snapshot.package_id != plan.package_id
            or snapshot.plan_sha256 != hashlib.sha256(canonical_json(data)).hexdigest()
            or snapshot.version != plan.version or snapshot.fingerprint != plan.fingerprint
            or snapshot.source_draft_revision != plan.source_draft_revision
            or type(snapshot.manifest_raw) is not bytes or len(snapshot.manifest_raw) > MAX_MANIFEST_BYTES
            or hashlib.sha256(snapshot.manifest_raw).hexdigest() != plan.source_draft_revision
            or type(snapshot.files) is not tuple or len(snapshot.files) >= 64):
        raise _error("promotion_population_plan_invalid")
    candidate = _decode(plan.manifest_raw, "manifest")
    draft = _source_manifest(snapshot.manifest_raw, data, candidate)
    lookup = {row.path: row for row in plan.files}
    expected = {row["path"]: lookup[row["path"]] for row in data["files"]}
    actual: dict[str, bytes] = {}
    total = 0
    for row in snapshot.files:
        if (type(row) is not PublicationSourceFile or type(row.path) is not str
                or row.path not in expected or row.path in actual or type(row.raw) is not bytes):
            raise _error("promotion_population_plan_invalid")
        target = expected[row.path]
        total += len(row.raw)
        if (total > MAX_FILE_BYTES or len(row.raw) != target.size
                or hashlib.sha256(row.raw).hexdigest() != target.sha256):
            raise _error("promotion_population_plan_invalid")
        actual[row.path] = row.raw
    if (list(actual) != list(expected) or publication_content_fingerprint(candidate, actual) != plan.fingerprint
            or publication_content_fingerprint(draft, actual) != plan.fingerprint):
        raise _error("promotion_population_plan_invalid")
    ordered = bounded_publication_paths(candidate, actual)
    rows = (("manifest.yaml", plan.manifest_raw),) + tuple((path, actual[path]) for path in ordered)
    folded: set[str] = set()
    for name, _raw in rows:
        parts = validate_relative("employee_packages/" + state.package_id + "/" + name)
        if not 3 <= len(parts) <= 6:
            raise _error("promotion_population_nested_unsupported")
        # Validate every name before ANY file creation, including NT USHORT size.
        if (name in (".", "..") or name.casefold() in folded
                or any(len(part.encode("utf-16-le")) > 65532 for part in parts[2:])):
            raise _error("promotion_population_plan_invalid")
        for part in parts[2:]:
            state.native._validate_component(part)
        folded.add(name.casefold())
    return plan.fingerprint, rows


def _population_abi(state: _State) -> None:
    native = state.native
    required = ("_FILE_READ_DATA", "_FILE_WRITE_DATA", "_FILE_NON_DIRECTORY_FILE",
                "_FILE_LIST_DIRECTORY", "_FILE_DIRECTORY_INFORMATION", "_FILE_DIRECTORY_INFORMATION_CLASS")
    if (any(getattr(native, name, None) is None for name in required)
            or not callable(getattr(state.workspace, "_file_info", None))):
        raise _error("promotion_population_native_unavailable")
    handle, dword = native.wintypes.HANDLE, native.wintypes.DWORD
    signatures = {
        "WriteFile": ([handle, ctypes.c_void_p, dword, ctypes.POINTER(dword), ctypes.c_void_p], ctypes.c_int),
        "ReadFile": ([handle, ctypes.c_void_p, dword, ctypes.POINTER(dword), ctypes.c_void_p], ctypes.c_int),
        "FlushFileBuffers": ([handle], ctypes.c_int),
        "SetFilePointerEx": ([handle, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), dword], ctypes.c_int),
        "GetFinalPathNameByHandleW": ([handle, native.wintypes.LPWSTR, dword, dword], dword),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(native._kernel32, name, None)
        if not callable(function):
            raise _error("promotion_population_native_unavailable")
        function.argtypes, function.restype = arguments, result
    query = getattr(native._ntdll, "NtQueryDirectoryFile", None)
    if not callable(query):
        raise _error("promotion_population_native_unavailable")
    query.argtypes = [handle, handle, ctypes.c_void_p, ctypes.c_void_p,
                     ctypes.POINTER(native._IO_STATUS_BLOCK), ctypes.c_void_p, ctypes.c_uint32,
                     ctypes.c_int, ctypes.c_ubyte, ctypes.c_void_p, ctypes.c_ubyte]
    query.restype = ctypes.c_int32


def _file_identity(state: _State, handle: int, size: int) -> tuple[int, int]:
    state.workspace._validate_handle(handle, state.workspace.root, directory=False)
    info = state.workspace._file_info(handle)
    names = ("dwFileAttributes", "dwVolumeSerialNumber", "nFileIndexHigh", "nFileIndexLow",
             "nFileSizeHigh", "nFileSizeLow", "nNumberOfLinks")
    values = tuple(getattr(info, name) for name in names)
    if any(type(value) is not int or not 0 <= value <= 0xFFFFFFFF for value in values):
        raise _error("promotion_population_unknown")
    attrs, volume, high, low, size_high, size_low, links = values
    identity = volume, (high << 32) | low
    if (attrs & (0x10 | 0x400) or links != 1 or not volume or not identity[1]
            or volume != state.workspace._identity(state.root)[0]
            or (size_high << 32) | size_low != size):
        raise _error("promotion_population_unknown")
    return identity


def _create_population_file(state: _State, name: str) -> int:
    native = state.native
    components = name.split("/")
    parent = state.stage
    for index, component in enumerate(components[:-1], 1):
        relative = "/".join(components[:index])
        pinned = state.population_directories.get(relative)
        if pinned is None:
            # Every directory is this attempt's exclusive FILE_CREATE object.
            # Never adopt an existing path, including a same-byte directory.
            handle = _open_directory(state, parent, component, create=True, delete_access=False)
            pinned = (handle, _check_directory(state, handle))
            state.population_directories[relative] = pinned
        if _check_directory(state, pinned[0]) != pinned[1]:
            raise _error("promotion_population_unknown")
        parent = pinned[0]
    name = components[-1]
    encoded = name.encode("utf-16-le")
    name_buffer = ctypes.create_unicode_buffer(name)
    name_value = native._UNICODE_STRING(
        Length=len(encoded), MaximumLength=len(encoded) + ctypes.sizeof(native.wintypes.WCHAR),
        Buffer=ctypes.cast(name_buffer, native.wintypes.LPWSTR))
    attributes = native._OBJECT_ATTRIBUTES(
        Length=ctypes.sizeof(native._OBJECT_ATTRIBUTES), RootDirectory=native.wintypes.HANDLE(parent),
        ObjectName=ctypes.pointer(name_value), Attributes=native._OBJ_CASE_INSENSITIVE,
        SecurityDescriptor=None, SecurityQualityOfService=None)
    result = native.wintypes.HANDLE()
    try:
        status = int(native._ntdll.NtCreateFile(
            ctypes.byref(result), native._FILE_READ_DATA | native._FILE_WRITE_DATA
            | native._FILE_READ_ATTRIBUTES | native._SYNCHRONIZE,
            ctypes.byref(attributes), ctypes.byref(native._IO_STATUS_BLOCK()), None,
            native._FILE_ATTRIBUTE_NORMAL, native._FILE_SHARE_READ, native._FILE_CREATE,
            native._FILE_NON_DIRECTORY_FILE | native._FILE_OPEN_REPARSE_POINT | native._FILE_SYNCHRONOUS_IO_NONALERT,
            None, 0))
    finally:
        handle = int(result.value) if result.value is not None else 0
        if handle and handle != native._INVALID_HANDLE_VALUE:
            state.handles.append(handle)
    if status != 0 or not handle or handle == native._INVALID_HANDLE_VALUE:
        raise _error("promotion_population_unknown")
    _file_identity(state, handle, 0)
    return handle


def _write_population_file(state: _State, handle: int, raw: bytes) -> None:
    native = state.native
    for offset in range(0, len(raw), _CHUNK_BYTES):
        chunk = raw[offset:offset + _CHUNK_BYTES]
        buffer = ctypes.create_string_buffer(chunk)
        count = native.wintypes.DWORD()
        if (not native._kernel32.WriteFile(native.wintypes.HANDLE(handle), buffer, len(chunk),
                                          ctypes.byref(count), None) or count.value != len(chunk)):
            raise _error("promotion_population_unknown")
    if not native._kernel32.FlushFileBuffers(native.wintypes.HANDLE(handle)):
        raise _error("promotion_population_unknown")


def _final_path(state: _State, handle: int) -> PureWindowsPath:
    # Do not reuse workspace._final_path's unbounded resizing loop.
    buffer = ctypes.create_unicode_buffer(_FINAL_PATH_CHARS)
    count = int(state.native._kernel32.GetFinalPathNameByHandleW(
        state.native.wintypes.HANDLE(handle), buffer, _FINAL_PATH_CHARS, 0))
    if not 0 < count < _FINAL_PATH_CHARS:
        raise _error("promotion_population_unknown")
    return PureWindowsPath(buffer.value)


def _read_population_file(state: _State, row: _PopulatedFile, stage_path: PureWindowsPath) -> None:
    native = state.native
    if _file_identity(state, row.handle, len(row.raw)) != row.identity:
        raise _error("promotion_population_unknown")
    file_path = _final_path(state, row.handle)
    # The name must still denote THIS held object, not a swapped same-size row.
    if file_path.parts != stage_path.parts + tuple(row.name.split("/")):
        raise _error("promotion_population_unknown")
    pointer = ctypes.c_longlong(-1)
    if (not native._kernel32.SetFilePointerEx(native.wintypes.HANDLE(row.handle), 0,
                                             ctypes.byref(pointer), 0) or pointer.value != 0):
        raise _error("promotion_population_unknown")
    digest = hashlib.sha256()
    for offset in range(0, len(row.raw), _CHUNK_BYTES):
        length = min(_CHUNK_BYTES, len(row.raw) - offset)
        buffer = ctypes.create_string_buffer(length)
        count = native.wintypes.DWORD()
        if (not native._kernel32.ReadFile(native.wintypes.HANDLE(row.handle), buffer, length,
                                         ctypes.byref(count), None) or count.value != length):
            raise _error("promotion_population_unknown")
        chunk = bytes(buffer)
        if chunk != row.raw[offset:offset + length]:
            raise _error("promotion_population_unknown")
        digest.update(chunk)
    buffer = ctypes.create_string_buffer(1)
    count = native.wintypes.DWORD()
    if (not native._kernel32.ReadFile(native.wintypes.HANDLE(row.handle), buffer, 1,
                                     ctypes.byref(count), None) or count.value != 0
            or digest.hexdigest() != row.sha256
            or _file_identity(state, row.handle, len(row.raw)) != row.identity
            or _final_path(state, row.handle).parts != stage_path.parts + tuple(row.name.split("/"))):
        raise _error("promotion_population_unknown")


def _enumerate_population(state: _State, handle: int | None = None) -> dict[str, int | None]:
    native = state.native
    result: dict[str, int | None] = {}
    folded: set[str] = set()
    dots: set[str] = set()
    for index in range(_DIRECTORY_QUERIES):
        buffer = ctypes.create_string_buffer(_CHUNK_BYTES)
        io = native._IO_STATUS_BLOCK()
        status = int(native._ntdll.NtQueryDirectoryFile(
            native.wintypes.HANDLE(state.stage if handle is None else handle), None, None, None, ctypes.byref(io),
            buffer, len(buffer), native._FILE_DIRECTORY_INFORMATION_CLASS, 1, None, index == 0)) & 0xFFFFFFFF
        if status == 0x80000006:
            if int(io.Information) != 0:
                raise _error("promotion_population_unknown")
            return result
        used = int(io.Information)
        structure = native._FILE_DIRECTORY_INFORMATION
        # WCHAR[1] introduces trailing C alignment padding; a short native row
        # need only contain its fixed header and actual filename bytes.
        if (status != 0 or not structure.FileName.offset <= used <= len(buffer)):
            raise _error("promotion_population_unknown")
        row = structure.from_buffer_copy(buffer)
        offset, length = structure.FileName.offset, int(row.FileNameLength)
        if (int(row.NextEntryOffset) != 0 or not 0 < length <= 65532 or length % 2
                or offset + length > used):
            raise _error("promotion_population_unknown")
        name = bytes(buffer)[offset:offset + length].decode("utf-16-le", errors="strict")
        if name in (".", ".."):
            if name in dots:
                raise _error("promotion_population_unknown")
            dots.add(name)
            continue
        if (len(result) >= _DIRECTORY_ROWS or name.casefold() in folded or name != str(PureWindowsPath(name))
                or len(PureWindowsPath(name).parts) != 1 or int(row.FileAttributes) & 0x400
                or not 0 <= int(row.EndOfFile) <= 22 * 1024 * 1024):
            raise _error("promotion_population_unknown")
        native._validate_component(name)
        result[name] = None if int(row.FileAttributes) & 0x10 else int(row.EndOfFile)
        folded.add(name.casefold())
    raise _error("promotion_population_unknown")


def _verify_population(state: _State, rows: tuple[_PopulatedFile, ...]) -> None:
    _verify_root_and_parents(state)
    if _check_directory(state, state.stage) != state.created_identity:
        raise _error("promotion_population_unknown")
    stage_path = _final_path(state, state.stage)
    expected: dict[str, dict[str, int | None]] = {"": {}}
    for name in state.population_directories:
        parent, _, leaf = name.rpartition("/")
        expected.setdefault(parent, {})[leaf] = None
        expected.setdefault(name, {})
    for row in rows:
        parent, _, leaf = row.name.rpartition("/")
        expected[parent][leaf] = len(row.raw)
    observed = {}
    for relative, entries in expected.items():
        handle, identity = (state.stage, state.created_identity) if not relative else state.population_directories[relative]
        expected_parts = stage_path.parts + (tuple(relative.split("/")) if relative else ())
        if (_check_directory(state, handle) != identity
                or _final_path(state, handle).parts != expected_parts):
            raise _error("promotion_population_unknown")
        observed[relative] = _enumerate_population(state, handle)
        if observed[relative] != entries:
            raise _error("promotion_population_unknown")
    for row in rows:
        _read_population_file(state, row, stage_path)
    # Rebuild actual native DFS row order (not sorted expected order). This
    # explicitly catches a destination filesystem that reorders a legacy skill.
    actual = {}
    by_name = {row.name: row.raw for row in rows}
    pending = [""]
    while pending:
        folder = pending.pop()
        child_directories = []
        for name, size in observed[folder].items():
            path = folder + "/" + name if folder else name
            if size is None:
                child_directories.append(path)
            elif path != "manifest.yaml":
                actual[path] = by_name[path]
        pending.extend(reversed(child_directories))
    from .employee_package_versions import _decode
    from .employee_publication_contents import publication_content_fingerprint
    manifest = _decode(state.population_manifest_raw, "manifest")
    if publication_content_fingerprint(manifest, actual) != manifest["package_fingerprint"]:
        raise _error("promotion_population_unknown")
    for relative, (handle, identity) in state.population_directories.items():
        if (_check_directory(state, handle) != identity
                or _final_path(state, handle).parts != stage_path.parts + tuple(relative.split("/"))
                or list(_enumerate_population(state, handle).items()) != list(observed[relative].items())):
            raise _error("promotion_population_unknown")
    if list(_enumerate_population(state).items()) != list(observed[""].items()):
        raise _error("promotion_population_unknown")
    if _check_directory(state, state.stage) != state.created_identity:
        raise _error("promotion_population_unknown")
    _verify_root_and_parents(state)


def populate_stage(witness: ExclusiveStage, *, plan: Any, snapshot: Any) -> None:
    """One bounded fill attempt; no return value grants permission or durability.

    The attempt is consumed before preflight, which can reject with zero file
    effects. Every subsequent failure leaves original partial files untouched.
    """
    if type(witness) is not ExclusiveStage:
        raise _error("promotion_witness_invalid")
    with _REGISTRY_LOCK:
        state = _ACTIVE.get(witness)
        if state is None or state.stage is None or state.attempted:
            raise _error("promotion_witness_invalid")
        if not state.for_population:
            raise _error("promotion_population_mode_invalid")
        if state.population_attempted:
            raise _error("promotion_population_lookup_only")
        state.population_attempted = True
    error_code: str | None = None
    preflight = True
    try:
        fingerprint, payload = _population_payload(state, plan, snapshot)
        state.population_manifest_raw = plan.manifest_raw
        preflight = False
        _population_abi(state)
        _verify_root_and_parents(state)
        if _check_directory(state, state.stage) != state.created_identity:
            raise _error("promotion_population_unknown")
        rows: list[_PopulatedFile] = []
        for name, raw in payload:
            handle = _create_population_file(state, name)
            identity = _file_identity(state, handle, 0)
            _write_population_file(state, handle, raw)
            row = _PopulatedFile(name, handle, identity, raw, hashlib.sha256(raw).hexdigest())
            _read_population_file(state, row, _final_path(state, state.stage))
            rows.append(row)
        complete = tuple(rows)
        _verify_population(state, complete)
        with _REGISTRY_LOCK:
            if _ACTIVE.get(witness) is not state or state.attempted:
                raise _error("promotion_population_unknown")
            state.population_files = complete
            state.population_fingerprint = fingerprint
            state.population_complete = True
        return
    except AIEmployeeContractError as exc:
        error_code = (exc.code if exc.code in ("promotion_population_plan_invalid",
                      "promotion_population_nested_unsupported", "promotion_population_native_unavailable")
                      else "promotion_population_plan_invalid" if preflight else "promotion_population_unknown")
    except Exception:
        error_code = "promotion_population_plan_invalid" if preflight else "promotion_population_unknown"
    except BaseException:
        with _REGISTRY_LOCK:
            state.population_complete = False
        raise
    with _REGISTRY_LOCK:
        state.population_complete = False
    # Raise outside handlers: no private lower-layer exception attached.
    raise _error(error_code)


def _rename_no_replace(state: _State, target_name: str) -> None:
    """The caller has released this attempt's child handles before this one call."""
    native = state.native
    encoded = target_name.encode("utf-16-le")
    # Native contract requires at least sizeof(struct) PLUS filename bytes,
    # not just the FileName offset. Filename length remains exact (no NUL).
    size = ctypes.sizeof(native._FILE_RENAME_INFO) + len(encoded)
    buffer = ctypes.create_string_buffer(size)
    info = ctypes.cast(buffer, ctypes.POINTER(native._FILE_RENAME_INFO)).contents
    info.ReplaceIfExists = 0
    info.RootDirectory = native.wintypes.HANDLE(state.target_chain[-1])
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + native._FILE_RENAME_INFO.FileName.offset,
                   encoded, len(encoded))
    status = int(native._ntdll.NtSetInformationFile(
        native.wintypes.HANDLE(state.stage), ctypes.byref(native._IO_STATUS_BLOCK()),
        buffer, size, native._FILE_RENAME_INFORMATION_CLASS))
    if status != 0:
        if status & 0xFFFFFFFF == _COLLISION:
            raise _error("promotion_target_exists")
        raise _error("promotion_outcome_unknown")


def _release_population_children(state: _State) -> None:
    """Release only owned descendants; root/stage/all ancestor pins stay held.

    Windows can reject a directory rename with any descendant handle open.
    Record identities/raw bytes remain immutable through this necessary gap.
    A failed or interrupted close consumes the attempt; it is never retried.
    """
    protected = {state.root, state.stage, *state.source_chain, *state.target_chain}
    owned = [row.handle for row in state.population_files]
    owned.extend(handle for handle, _identity in state.population_directories.values())
    if len(owned) != len(set(owned)) or any(handle in protected or handle not in state.handles for handle in owned):
        raise _error("promotion_outcome_unknown")
    for row in reversed(state.population_files):
        _close(state, row.handle)
    # Population always records parents before their newly created children.
    for handle, _identity in reversed(tuple(state.population_directories.values())):
        _close(state, handle)


def _open_population_file_readonly(state: _State, parent: int, name: str) -> int:
    """Open one existing child relative to its verified directory, no writes/deletes."""
    native = state.native
    native._validate_component(name)
    encoded = name.encode("utf-16-le")
    name_buffer = ctypes.create_unicode_buffer(name)
    name_value = native._UNICODE_STRING(
        Length=len(encoded), MaximumLength=len(encoded) + ctypes.sizeof(native.wintypes.WCHAR),
        Buffer=ctypes.cast(name_buffer, native.wintypes.LPWSTR))
    attributes = native._OBJECT_ATTRIBUTES(
        Length=ctypes.sizeof(native._OBJECT_ATTRIBUTES), RootDirectory=native.wintypes.HANDLE(parent),
        ObjectName=ctypes.pointer(name_value), Attributes=native._OBJ_CASE_INSENSITIVE,
        SecurityDescriptor=None, SecurityQualityOfService=None)
    result = native.wintypes.HANDLE()
    try:
        status = int(native._ntdll.NtCreateFile(
            ctypes.byref(result), native._FILE_READ_DATA | native._FILE_READ_ATTRIBUTES | native._SYNCHRONIZE,
            ctypes.byref(attributes), ctypes.byref(native._IO_STATUS_BLOCK()), None,
            native._FILE_ATTRIBUTE_NORMAL, native._FILE_SHARE_READ, native._FILE_OPEN,
            native._FILE_NON_DIRECTORY_FILE | native._FILE_OPEN_REPARSE_POINT | native._FILE_SYNCHRONOUS_IO_NONALERT,
            None, 0))
    finally:
        handle = int(result.value) if result.value is not None else 0
        if handle and handle != native._INVALID_HANDLE_VALUE:
            state.handles.append(handle)
    if status != 0 or not handle or handle == native._INVALID_HANDLE_VALUE:
        raise _error("promotion_outcome_unknown")
    return handle


def _repin_population_children(state: _State) -> None:
    """Reopen the original bounded tree; unchanged bytes cannot mask a new object."""
    original_directories = state.population_directories
    reopened: dict[str, tuple[int, tuple[int, int]]] = {}
    for relative, (_old_handle, identity) in original_directories.items():
        parent, _, name = relative.rpartition("/")
        parent_handle = state.stage if not parent else reopened[parent][0]
        handle = _open_directory(state, parent_handle, name, delete_access=False, list_access=True)
        if _check_directory(state, handle) != identity:
            raise _error("promotion_outcome_unknown")
        reopened[relative] = handle, identity
    rows = []
    for row in state.population_files:
        parent, _, name = row.name.rpartition("/")
        parent_handle = state.stage if not parent else reopened[parent][0]
        handle = _open_population_file_readonly(state, parent_handle, name)
        if _file_identity(state, handle, len(row.raw)) != row.identity:
            raise _error("promotion_outcome_unknown")
        rows.append(_PopulatedFile(row.name, handle, row.identity, row.raw, row.sha256))
    state.population_directories = reopened
    state.population_files = tuple(rows)
    # Recheck full exact directory sets, final paths, sizes, original identities,
    # original bytes and the legacy-ordered package/skill fingerprints twice.
    # No receipt is returned merely because the top directory identity matched.
    _verify_population(state, state.population_files)


def promote_stage(witness: ExclusiveStage, *, fingerprint: str) -> PromotionReceipt:
    """One attempt, fixed .versions/<SHA>; no target precheck or overwrite."""
    fingerprint = _identifier(fingerprint, _SHA)
    if type(witness) is not ExclusiveStage:
        raise _error("promotion_witness_invalid")
    with _REGISTRY_LOCK:
        state = _ACTIVE.get(witness)
        if state is None:
            raise _error("promotion_witness_invalid")
        if state.attempted:
            raise _error("promotion_lookup_only")
        if state.stage is None or state.created_identity is None:
            raise _error("promotion_witness_invalid")
        if state.for_population and (not state.population_complete
                                     or state.population_fingerprint != fingerprint):
            raise _error("promotion_population_incomplete")
        state.attempted = True
    try:
        if state.for_population:
            _verify_population(state, state.population_files)
        _verify_root_and_parents(state)
        if _check_directory(state, state.stage) != state.created_identity:
            raise _error("promotion_outcome_unknown")
        # Do not open stage a second time: the held DELETE access conflicts with
        # an additional deny-SHARE_DELETE open. Its own retained native handle,
        # pinned parent chain and denied delete sharing protect this entry.
        root_volume = state.workspace._identity(state.root)[0]
        if (state.created_identity[0] != root_volume
                or state.workspace._identity(state.target_chain[-1])[0] != root_volume):
            raise _error("promotion_outcome_unknown")
        if state.for_population:
            _release_population_children(state)
        _rename_no_replace(state, fingerprint)
        # Never relax sharing policy to verify the target. Close the original
        # DELETE object first; replacement in this interval fails identity check.
        source_handle = state.stage
        state.stage = None
        _close(state, source_handle)
        target = _open_directory(state, state.target_chain[-1], fingerprint, list_access=state.for_population)
        if _check_directory(state, target) != state.created_identity:
            raise _error("promotion_outcome_unknown")
        state.stage = target
        if state.for_population:
            _repin_population_children(state)
        _verify_root_and_parents(state)
        return PromotionReceipt(state.package_id, state.request_key, fingerprint,
                                _identity=state.created_identity)
    except AIEmployeeContractError as exc:
        if exc.code == "promotion_target_exists":
            raise
        raise _error("promotion_outcome_unknown") from None
    except Exception:
        raise _error("promotion_outcome_unknown") from None
