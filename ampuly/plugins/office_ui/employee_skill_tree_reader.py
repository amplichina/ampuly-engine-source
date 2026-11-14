"""Bounded native read-only tree evidence for immutable employee package skills.

Match skill_replacement.skill_tree_fingerprint: raw byte digests, native walk
order (files before child directories), then its compact UTF-8 JSON row hash.
No Skill is parsed/imported/executed, no directory or lock is created. A tree
digest is content evidence, not an independent publication tail or authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from ampuly.layer4_tools.workspace_fs import WorkspaceBoundaryError, WorkspacePath
from .ai_employee_contract import AIEmployeeContractError
from .ai_employee_safe_files import AIEmployeeSafeFiles, validate_relative

MAX_FILES = 500
MAX_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DIRECTORIES = 500
MAX_ENTRIES = 1000
MAX_DEPTH = 32
MAX_RELATIVE_CHARACTERS = 4096
MAX_REQUEST_READ_BYTES = 64 * 1024 * 1024
MAX_REQUEST_ENTRIES = 10_000
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_SKILL = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


def _fail(reason: str) -> None:
    raise AIEmployeeContractError("immutable_skill_tree_unverified", "技能整树依据暂不能安全核实。",
                                  details={"reason": reason, "effects_produced": False}) from None


def _tree_parts(relative: str) -> tuple[str, ...]:
    try:
        parts = validate_relative(relative)
    except AIEmployeeContractError:
        _fail("invalid_path")
    if (len(parts) != 6 or parts[0] != "employee_packages" or parts[2] != ".versions" or parts[4] != "skills"
            or not _PACKAGE.fullmatch(parts[1]) or not _SHA.fullmatch(parts[3]) or not _SKILL.fullmatch(parts[5])):
        _fail("invalid_path")
    return parts


def _file_stamp(fd: int) -> tuple[int, ...]:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _fail("invalid_entry")
    return (int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns),
            int(info.st_ctime_ns), int(info.st_mode), int(info.st_nlink))


@contextmanager
def _directory(workspace: Any, target: WorkspacePath) -> Iterator[int]:
    handle = workspace._open_directory_handle(target) if os.name == "nt" else workspace.open_directory(target)
    try:
        yield handle
    finally:
        workspace._close_handle(handle) if os.name == "nt" else os.close(handle)


def _directory_stamp(workspace: Any, handle: int) -> tuple[int, ...]:
    if os.name == "nt":
        info = workspace._file_info(handle)
        return (*workspace._identity(handle), int(info.dwFileAttributes),
                int(info.ftCreationTime.dwHighDateTime), int(info.ftCreationTime.dwLowDateTime),
                int(info.ftLastWriteTime.dwHighDateTime), int(info.ftLastWriteTime.dwLowDateTime))
    info = os.fstat(handle)
    if not stat.S_ISDIR(info.st_mode):
        _fail("invalid_entry")
    return (int(info.st_dev), int(info.st_ino), int(info.st_mtime_ns), int(info.st_ctime_ns), int(info.st_mode))


class EmployeeSkillTreeReader:
    """Host-bound, request-local callback; never share it across RPCs."""
    def __init__(self, root: Path, *, root_guard: Callable[[Any], None] | None = None) -> None:
        if not isinstance(root, (str, Path)) or not str(root) or not Path(root).is_absolute():
            _fail("invalid_root")
        if root_guard is not None and not callable(root_guard):
            _fail("invalid_root_guard")
        self.files = AIEmployeeSafeFiles(Path(root), root_guard=root_guard)
        self.request_read_bytes = 0
        self.request_entries = 0

    def _read_hash(self, workspace: Any, target: WorkspacePath, remaining: int, *, entry: bool) -> tuple[str, tuple[int, ...]]:
        fd = workspace.open_read(target)
        try:
            before = _file_stamp(fd)
            remaining = min(remaining, MAX_FILE_BYTES)
            if before[2] > remaining:
                _fail("limit_exceeded")
            if entry and before[2] == 0:
                _fail("empty_skill_entry")
            digest, size = hashlib.sha256(), 0
            while True:
                if size == before[2]:
                    break
                available = MAX_REQUEST_READ_BYTES - self.request_read_bytes
                if available <= 0:
                    _fail("request_limit_exceeded")
                raw = os.read(fd, min(64 * 1024, remaining - size + 1, available))
                if not raw:
                    break
                self.request_read_bytes += len(raw)
                size += len(raw)
                if size > remaining:
                    _fail("limit_exceeded")
                digest.update(raw)
            if size != before[2] or _file_stamp(fd) != before:
                _fail("read_changed")
            current = workspace.open_read(target)
            try:
                if _file_stamp(current) != before:
                    _fail("read_changed")
            finally:
                os.close(current)
            return digest.hexdigest(), before
        finally:
            os.close(fd)

    def _scan(self, workspace: Any, relative: str) -> tuple[list[tuple[str, str]], dict[str, tuple[int, ...]], dict[str, tuple[int, ...]]]:
        rows: list[tuple[str, str]] = []
        directories: dict[str, tuple[int, ...]] = {}
        files: dict[str, tuple[int, ...]] = {}
        pending, total, entries = [(relative, 0)], 0, 0
        while pending:
            folder, depth = pending.pop()
            if depth > MAX_DEPTH or len(directories) >= MAX_DIRECTORIES:
                _fail("limit_exceeded")
            target = self.files.target(workspace, folder)
            with _directory(workspace, target) as handle:
                before = _directory_stamp(workspace, handle)
                # These existing native helpers reject links/special entries.
                # Their unsorted result preserves the legacy os.walk row order;
                # public list_entries sorts, and therefore is NOT used here.
                if self.request_entries >= MAX_REQUEST_ENTRIES:
                    _fail("request_limit_exceeded")
                # One extra entry is the bounded overflow witness. Do not
                # enumerate a whole tree batch after request budget is spent.
                entry_limit = min(MAX_ENTRIES - entries, MAX_REQUEST_ENTRIES - self.request_entries) + 1
                children = (self.files._list_windows(workspace, target, entry_limit)
                            if os.name == "nt" else self.files._list_posix(workspace, target, entry_limit))
                entries += len(children)
                self.request_entries += len(children)
                if self.request_entries > MAX_REQUEST_ENTRIES:
                    _fail("request_limit_exceeded")
                if entries > MAX_ENTRIES or len({child["name"] for child in children}) != len(children):
                    _fail("limit_exceeded")
                if _directory_stamp(workspace, handle) != before:
                    _fail("read_changed")
                directories[folder] = before
                subdirectories: list[tuple[str, int]] = []
                for child in children:
                    child_relative = folder + "/" + child["name"]
                    if len(child_relative) > MAX_RELATIVE_CHARACTERS:
                        _fail("limit_exceeded")
                    child_target = self.files.target(workspace, child_relative)
                    if child["kind"] == "directory":
                        subdirectories.append((child_relative, depth + 1))
                        continue
                    if child["kind"] != "file":
                        _fail("invalid_entry")
                    if len(rows) >= MAX_FILES:
                        _fail("limit_exceeded")
                    local = child_relative[len(relative) + 1:]
                    digest, stamp = self._read_hash(workspace, child_target, MAX_BYTES - total, entry=local == "SKILL.md")
                    total += stamp[2]
                    rows.append((local, digest)); files[child_relative] = stamp
                with _directory(workspace, target) as current:
                    if _directory_stamp(workspace, current) != before or _directory_stamp(workspace, handle) != before:
                        _fail("read_changed")
                # Top-down os.walk hashes this folder's files before all children.
                pending.extend(reversed(subdirectories))
        if not rows or relative + "/SKILL.md" not in files:
            _fail("missing_skill_entry")
        return rows, directories, files

    def _verify_names(self, workspace: Any, directories: dict[str, tuple[int, ...]], files: dict[str, tuple[int, ...]]) -> None:
        # Final bounded name/metadata fence for files read early in the second
        # sweep. This detects observed changes, not an atomic/ABA-proof snapshot.
        for relative, expected in files.items():
            fd = workspace.open_read(self.files.target(workspace, relative))
            try:
                if _file_stamp(fd) != expected:
                    _fail("read_changed")
            finally:
                os.close(fd)
        for relative, expected in directories.items():
            with _directory(workspace, self.files.target(workspace, relative)) as handle:
                if _directory_stamp(workspace, handle) != expected:
                    _fail("read_changed")

    def fingerprint(self, relative: str) -> str:
        _tree_parts(relative)
        try:
            with self.files.workspace() as workspace:
                first = self._scan(workspace, relative)
                second = self._scan(workspace, relative)
                if first != second:
                    _fail("read_changed")
                self._verify_names(workspace, second[1], second[2])
                encoded = json.dumps(first[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                return hashlib.sha256(encoded).hexdigest()
        except FileNotFoundError:
            _fail("missing_tree_or_entry")
        except WorkspaceBoundaryError:
            _fail("native_or_boundary_rejected")
        except AIEmployeeContractError as exc:
            # Never propagate a lower layer's path/details or private body.
            reason = exc.details.get("reason") if isinstance(exc.details, dict) else None
            if exc.code == "immutable_skill_tree_unverified" and reason in {
                "limit_exceeded", "request_limit_exceeded", "invalid_entry", "empty_skill_entry", "missing_skill_entry", "read_changed"
            }:
                raise
            if exc.code == "storage_record_invalid":
                _fail("limit_exceeded")
            _fail("invalid_entry")
        except (OSError, UnicodeError, ValueError):
            _fail("read_unavailable")

    __call__ = fingerprint
