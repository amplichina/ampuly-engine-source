"""Bounded saved-workflow reads using the existing native workspace capability.

The root is pinned once, links are refused, and limits are checked before
reading bytes or descending further. No asset store authority is expanded.
"""
from __future__ import annotations

import os
from pathlib import Path

from ampuly.layer4_tools.workspace_fs import WorkspaceBoundaryError, _read_bounded_fd
from .ai_employee_contract import AIEmployeeContractError
from .ai_employee_safe_files import AIEmployeeSafeFiles

MAX_ENTRIES = 128
MAX_DEPTH = 8


def read_workflow_tree(root: Path, *, max_files: int, max_bytes: int) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = entries_seen = 0
    safe = AIEmployeeSafeFiles(root)

    def too_large():
        raise AIEmployeeContractError("workflow_snapshot_too_large", "业务流目录超过冻结范围。")

    try:
        with safe.workspace() as workspace:
            pending = [("", 0)]
            listings = []
            listing = safe._list_windows if os.name == "nt" else safe._list_posix
            while pending:
                relative, depth = pending.pop()
                if depth > MAX_DEPTH or entries_seen >= MAX_ENTRIES:
                    too_large()
                target = workspace.resolve(relative, use_output_root=False)
                rows = listing(workspace, target, MAX_ENTRIES - entries_seen)
                entries_seen += len(rows)
                listings.append((target, rows))
                for row in sorted(rows, key=lambda value: value["name"]):
                    if row["name"].startswith("."):
                        continue
                    child = "/".join(filter(None, [relative, row["name"]]))
                    if row["kind"] == "directory":
                        pending.append((child, depth + 1))
                        continue
                    if len(files) >= max_files:
                        too_large()
                    path = workspace.resolve(child, use_output_root=False)
                    fd = workspace.open_read(path)
                    try:
                        before = os.fstat(fd)
                        if before.st_size > max_bytes - total:
                            too_large()
                        raw = _read_bounded_fd(fd, max(1, max_bytes - total))
                        after = os.fstat(fd)
                        if before.st_mtime_ns != after.st_mtime_ns:
                            raise WorkspaceBoundaryError("Workflow changed during read")
                        current = workspace.open_read(path)
                        try:
                            info = os.fstat(current)
                            if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
                                raise WorkspaceBoundaryError("Workflow name changed during read")
                        finally:
                            os.close(current)
                    finally:
                        os.close(fd)
                    total += len(raw)
                    if total > max_bytes:
                        too_large()
                    raw.decode("utf-8")
                    files[child] = raw
            # Reject observed membership changes; this is not a global atomic
            # filesystem snapshot, and callers still freeze the bytes returned.
            for target, rows in listings:
                if sorted(listing(workspace, target, MAX_ENTRIES), key=lambda r: r["name"]) != sorted(rows, key=lambda r: r["name"]):
                    raise WorkspaceBoundaryError("Workflow membership changed during read")
    except FileNotFoundError:
        raise AIEmployeeContractError("workflow_not_found", "没有找到这条已保存的业务流。") from None
    except AIEmployeeContractError as exc:
        if exc.code == "storage_record_invalid":
            too_large()
        raise
    except (OSError, UnicodeError, WorkspaceBoundaryError):
        raise AIEmployeeContractError("workflow_snapshot_invalid", "业务流目录包含链接、变动或无法冻结的文本对象。") from None
    if "workflow.yaml" not in files:
        raise AIEmployeeContractError("workflow_not_found", "没有找到这条已保存的业务流。")
    return files
