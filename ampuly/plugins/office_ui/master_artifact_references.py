"""Bounded artifact excerpts in the existing master-message reference contract.

The server chooses the project root. A preview revision identifies the opened
file and its bounded prefix, not an entire large-file digest or an execution
permission. Sending reopens through native workspace handles and checks that
revision before capturing the excerpt into the original message's metadata.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from ampuly.layer4_tools.workspace_fs import SecureWorkspace

PREVIEW_BYTES = 60_000
MAX_SELECTION_UNITS = 4_000  # JavaScript textarea offsets, UTF-16 code units.


def _file_stamp(fd: int) -> tuple[int, ...]:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("artifact_reference_unavailable")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def read_reference_preview(engine: Any, project_id: str, path: str) -> dict[str, Any]:
    from .artifacts_service import _PREVIEW_BYTES, _TEXT_SUFFIXES, _hidden, artifact_path_parts, workspace_root_for_project

    if type(path) is not str or len(path) > 1024:
        raise ValueError("artifact_reference_path_invalid")
    parts = artifact_path_parts(path)
    relative = Path(*parts)
    if _hidden(relative) or relative.suffix.lower() not in _TEXT_SUFFIXES:
        raise ValueError("artifact_reference_text_required")
    canonical = relative.as_posix()
    root = workspace_root_for_project(engine, project_id)
    preview_bytes = min(PREVIEW_BYTES, _PREVIEW_BYTES)
    # Deliberately do not opt into the lexical fallback: it cannot attest the
    # check/use boundary required by this new quote contract.
    with SecureWorkspace(str(root), str(root)) as workspace:
        target = workspace.resolve(canonical, use_output_root=False)
        fd = workspace.open_read(target)
        try:
            before = _file_stamp(fd)
            chunks = bytearray()
            while len(chunks) <= preview_bytes:
                chunk = os.read(fd, min(16_384, preview_bytes + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            current = workspace.open_read(target)
            try:
                if (_file_stamp(fd) != before or _file_stamp(current) != before
                        or len(chunks) != min(before[2], preview_bytes + 1)):
                    raise ValueError("artifact_reference_changed")
            finally:
                os.close(current)
        finally:
            os.close(fd)
    raw = bytes(chunks)
    identity = json.dumps(
        ["artifact-preview-v1", project_id, canonical, before], ensure_ascii=True, separators=(",", ":"),
    ).encode("ascii")
    # Canonical newlines make textarea offsets portable across Windows/browsers.
    content = raw[:preview_bytes].decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return {
        "path": canonical, "name": relative.name, "kind": "text", "size": before[2],
        "absolute_path": str(root.joinpath(*parts)),
        "modified_at": datetime.fromtimestamp(before[3] / 1_000_000_000).isoformat(timespec="seconds"),
        "content": content, "truncated": before[2] > preview_bytes,
        "preview_revision": hashlib.sha256(identity + b"\0" + raw).hexdigest(),
        "selection_encoding": "utf-16", "selection_limit": MAX_SELECTION_UNITS,
    }


def resolve_artifact_reference(engine: Any, project_id: str, item: dict[str, Any]) -> dict[str, Any]:
    """Resolve only after the caller's normal project/customer permission check."""
    if type(item.get("project_id")) is not str or item["project_id"] != project_id:
        raise ValueError("artifact_reference_scope_mismatch")
    revision = item.get("preview_revision")
    start, end = item.get("selection_start"), item.get("selection_end")
    if (type(revision) is not str or re.fullmatch(r"[0-9a-f]{64}", revision) is None
            or type(start) is not int or type(end) is not int or start < 0 or end <= start
            or end - start > MAX_SELECTION_UNITS or end > PREVIEW_BYTES):
        raise ValueError("artifact_reference_selection_invalid")
    preview = read_reference_preview(engine, project_id, item.get("path"))
    if preview["preview_revision"] != revision:
        raise ValueError("artifact_reference_changed")
    utf16 = preview["content"].encode("utf-16-le")
    if end * 2 > len(utf16):
        raise ValueError("artifact_reference_selection_invalid")
    try:
        quote = utf16[start * 2:end * 2].decode("utf-16-le")
    except UnicodeError:
        raise ValueError("artifact_reference_selection_invalid") from None
    if not quote.strip():
        raise ValueError("artifact_reference_selection_empty")
    return {
        "kind": "artifact", "project_id": project_id, "path": preview["path"],
        "artifact_name": preview["name"], "preview_revision": revision,
        "selection_start": start, "selection_end": end, "selection_encoding": "utf-16",
        "selection_text": quote, "preview_truncated": preview["truncated"],
    }
