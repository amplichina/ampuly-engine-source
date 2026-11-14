"""Deliverable index for the office UI (交付物).

Every station writes its output into the project workplace. This module turns
that directory into a browsable index: what each role produced, where it
lives, and enough content to preview it in the front end.

The filesystem is the source of truth. An artifact table exists in the store
but depends on agents self-reporting their outputs, which is unreliable; a
file that exists on disk is a fact. Runtime-internal state (``.ampuly-comms``,
temp files) is never exposed.

Plugin-layer module: no engine-kernel changes.
"""

from __future__ import annotations

import os
import hashlib
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from ampuly.core.config import get_project_workplace
from ampuly.project_id import is_valid_project_id

# Runtime-owned paths that are plumbing, not deliverables.
_HIDDEN_PREFIXES = (".ampuly-comms", ".ampuly-write-", ".git", "__pycache__", "node_modules")

_PREVIEW_BYTES = 60_000
_MAX_FILES = 500
_MAX_SCAN_ENTRIES = 5000
_MAX_IMAGE_BYTES = 8 * 1024 * 1024

_TEXT_SUFFIXES = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".html", ".svg",
    ".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".sql", ".xml", ".log",
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def artifact_path_parts(relative_path: str) -> tuple[str, ...]:
    """Use the same unambiguous relative spelling for scope checks and reads.

    The whole string is *not* stripped. It was until 2026-09-23, and the scope rule beside it (``data_scope``)
    was not: one leading space made the scope classify the path as nothing in particular -- no order, no area,
    nothing hidden -- while this function stripped it and opened the very file the scope had just refused. A
    component with whitespace or a dot at either end is a different name on Windows, so it is refused here
    rather than repaired.
    """
    candidate = str(relative_path or "").replace("\\", "/")
    if not candidate:
        raise ValueError("path required")
    parts = tuple(candidate.split("/"))
    if ":" in candidate or "\x00" in candidate or any(
        part in {"", ".", ".."} or part.strip() != part or part.rstrip(".") != part for part in parts
    ):
        raise ValueError("path outside the project workspace or noncanonical")
    return parts


# A Windows reparse point (a junction, a mount point, a symlink): the attribute bit, so the check does not depend on
# ``Path.is_junction``, which only exists from Python 3.12 while this project still allows 3.10.
_REPARSE_POINT = 0x400


def redirected(path: Path) -> bool:
    """Whether this exact name is a link of any kind -- a symlink, a junction or any other reparse point.

    Asked of the name itself (``lstat``), never of what it points at: a component that redirects is refused before the
    path is opened, so a folder swapped for a link to another order cannot be read through it (data phase 2, D1).
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        # A name that is not there is not a link to anywhere: the caller answers "not found" as it always did.
        # Failing closed here told 安安 that a missing order folder was "reached through a link" (found by the
        # phase-2 full run, 2026-09-23). ``lstat`` does not follow, so a broken link still answers below.
        return False
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT):
        return True
    return bool(getattr(path, "is_junction", lambda: False)())


def redirected_entry(entry: os.DirEntry) -> bool:
    """The same test for an entry of a scan, using the stat the scan already has."""
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT):
        return True
    return bool(getattr(Path(entry.path), "is_junction", lambda: False)())


def multiply_linked(path: Path) -> bool:
    """Whether this file has more than one name on disk: a hard link can carry a file from outside in (D1).

    ``DirEntry.stat`` reports no link count on Windows, so this asks the file system itself.
    """
    try:
        return int(os.stat(path, follow_symlinks=False).st_nlink) != 1
    except OSError:
        return True


def confined_dir(base: Path, parts: tuple[str, ...]) -> Path:
    """``base`` joined with ``parts``, proven to be exactly that folder: no component redirects, and the real path
    below the real base is spelled the way it was asked for (data phase 2, D1)."""
    root = Path(base)
    if redirected(root):
        raise ValueError("the folder is reached through a link")
    target = root
    for part in parts:
        target = target / part
        if redirected(target):
            raise ValueError("the folder is reached through a link")
    real_base = Path(os.path.realpath(root))
    try:
        walked = Path(os.path.realpath(target)).relative_to(real_base).parts
    except ValueError as exc:
        raise ValueError("the folder is outside its root") from exc
    if walked != tuple(parts):
        raise ValueError("the folder is reached through a link")
    return target


def _hidden(relative: Path) -> bool:
    return any(
        part.startswith(_HIDDEN_PREFIXES) or part.startswith(".")
        for part in relative.parts
    )


def _kind_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _TEXT_SUFFIXES:
        return "text"
    return "binary"


def workspace_root_for_project(engine: Any, project_id: str) -> Path:
    """Resolve the directory a project's roles actually write into."""
    normalized_project_id = project_id or "default"
    if not is_valid_project_id(normalized_project_id):
        raise ValueError("invalid project id")
    configured = str(
        getattr(getattr(engine, "config", None), "system", None)
        and getattr(engine.config.system, "workplace_root", "")
        or ""
    ).strip()
    if configured:
        return Path(configured).expanduser() / normalized_project_id
    return get_project_workplace(normalized_project_id)


def build_artifact_index(engine: Any, project_id: str) -> dict[str, Any]:
    """List deliverables on disk, grouped by their top-level station folder."""
    # Resolve once up front so the index cannot advertise a symlink/junction
    # that points outside the project workspace.  ``read_artifact`` applies
    # the same boundary check, but filtering here keeps the list truthful and
    # avoids presenting files that the preview action must reject later.
    workspace = workspace_root_for_project(engine, project_id)
    if redirected(workspace) or redirected(workspace.parent):
        raise ValueError("project data area is reached through a link")
    root = workspace.resolve()
    files: list[dict[str, Any]] = []
    truncated = False

    pending = [root] if root.is_dir() else []
    scanned = 0
    while pending and not truncated:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > _MAX_SCAN_ENTRIES or len(files) >= _MAX_FILES:
                        truncated = True
                        break
                    absolute = Path(entry.path)
                    relative = absolute.relative_to(root)
                    # Prune before descending, not after materializing rglob.
                    if _hidden(relative) or redirected_entry(entry):
                        continue
                    try:
                        absolute.resolve().relative_to(root)
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(absolute)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        # A file with a second name on disk may live outside this project (D1).
                        if multiply_linked(absolute):
                            continue
                        info = entry.stat(follow_symlinks=False)
                    except (OSError, ValueError):
                        continue
                    parts = relative.parts
                    files.append({
                        "path": relative.as_posix(), "name": absolute.name,
                        "group": parts[0] if len(parts) > 1 else "",
                        "kind": _kind_for(absolute), "size": int(info.st_size),
                        "modified_at": datetime.fromtimestamp(info.st_mtime).isoformat(timespec="seconds"),
                    })
        except OSError:
            truncated = True

    files.sort(key=lambda item: item["modified_at"], reverse=True)
    groups: dict[str, int] = {}
    for item in files:
        groups[item["group"]] = groups.get(item["group"], 0) + 1

    return {
        "project_id": project_id,
        "workspace_root": str(root),
        "files": files,
        "groups": [
            {"name": name, "count": count}
            for name, count in sorted(groups.items(), key=lambda kv: kv[0])
        ],
        "truncated": truncated,
    }


def read_artifact(engine: Any, project_id: str, relative_path: str) -> dict[str, Any]:
    """Return one deliverable's content for preview.

    The path is resolved inside the project workspace and re-checked after
    resolution, so a crafted relative path cannot read outside it.
    """
    parts = artifact_path_parts(relative_path)
    if Path(parts[-1]).suffix.lower() in _TEXT_SUFFIXES:
        from .master_artifact_references import read_reference_preview

        # Preserve the public scope refusals; native handles remain the final
        # authority if any name changes after these explanatory checks.
        target = workspace_root_for_project(engine, project_id)
        if redirected(target.parent) or redirected(target):
            raise ValueError("artifact path contains a redirected component")
        for part in parts:
            target = target / part
            if redirected(target):
                raise ValueError("artifact path contains a redirected component")
        if not target.is_file():
            raise FileNotFoundError("/".join(parts))
        if multiply_linked(target):
            raise ValueError("multiply-linked file")
        return read_reference_preview(engine, project_id, relative_path)
    return preview_file_under(workspace_root_for_project(engine, project_id), relative_path)


def preview_file_under(workspace: Path, relative_path: str) -> dict[str, Any]:
    """The same bounded preview for any workspace root -- a project's, or one isolated trial's."""
    root = Path(workspace).resolve()
    parts = artifact_path_parts(relative_path)
    candidate = "/".join(parts)
    if redirected(root):
        raise ValueError("artifact path contains a redirected component")
    target = root
    for part in parts:
        target = target / part
        if redirected(target):
            raise ValueError("artifact path contains a redirected component")
    target = target.resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError("path outside the workspace") from exc
    if _hidden(relative):
        raise ValueError("path is runtime-internal")
    if not target.is_file():
        raise FileNotFoundError(candidate)
    if multiply_linked(target):
        raise ValueError("multiply-linked file")

    info = target.stat()
    kind = _kind_for(target)
    payload: dict[str, Any] = {
        "path": relative.as_posix(),
        "name": target.name,
        "kind": kind,
        "size": int(info.st_size),
        "absolute_path": str(target),
        "modified_at": datetime.fromtimestamp(info.st_mtime).isoformat(timespec="seconds"),
    }
    if kind == "text":
        try:
            with target.open("rb") as stream:
                raw = stream.read(_PREVIEW_BYTES + 1)
        except OSError as exc:
            raise ValueError(f"could not read file: {exc}") from exc
        payload["truncated"] = len(raw) > _PREVIEW_BYTES
        payload["content"] = raw[:_PREVIEW_BYTES].decode("utf-8", errors="replace")
    elif kind == "image":
        import base64

        try:
            with target.open("rb") as stream:
                raw = stream.read(_MAX_IMAGE_BYTES + 1)
            if len(raw) > _MAX_IMAGE_BYTES:
                raise ValueError("image exceeds preview byte limit")
            payload["data_url"] = (
                f"data:image/{target.suffix.lstrip('.').lower()};base64,"
                + base64.b64encode(raw).decode("ascii")
            )
        except OSError as exc:
            raise ValueError(f"could not read image: {exc}") from exc
    logger.debug(f"artifacts: previewed {relative.as_posix()} ({kind})")
    return payload


def artifact_evidence(engine: Any, project_id: str, relative_path: str) -> dict[str, Any]:
    """Bind one workspace file to acceptance evidence by content, not name."""
    payload = read_artifact(engine, project_id, relative_path)
    target = Path(str(payload["absolute_path"]))
    before = target.stat()
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    after = target.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("artifact changed while acceptance evidence was recorded")
    return {
        "artifact_id": f"sha256:{digest.hexdigest()}",
        "artifact_hash": digest.hexdigest(),
        "artifact_path": str(payload["path"]),
        "artifact_size": int(after.st_size),
    }


def reveal_artifact(engine: Any, project_id: str, relative_path: str) -> str:
    """Open the deliverable's folder in the OS file manager. Returns the path."""
    payload = read_artifact(engine, project_id, relative_path)
    target = Path(payload["absolute_path"])
    if os.name == "nt":
        os.startfile(str(target.parent))  # noqa: S606 - local operator action
    return str(target.parent)
