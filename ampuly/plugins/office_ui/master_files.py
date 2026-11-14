"""What master control may read from disk, and nothing else.

Master control had eight tools and none of them could open a file, so the
plan's headline scenario -- check a file, change the candidate, check again
(A02) -- could not begin. Giving it a general file tool would hand a model
the operator's disk; giving it nothing leaves R1 unreachable. The owner's
decision on 2026-09-11 was the middle: the candidate process packages, and
the deliverables of one named order. This module is that boundary.

Two roots, each resolved by the product rather than by the model:

- **A candidate package.** A workflow package in `draft` or `testing`
  status, looked up by id among the packages the product itself discovers.
  A published package is production and is not offered; the model cannot
  name a directory, only an id the product recognises.
- **One order's deliverables.** `orders/<task_id>/` inside the project's
  workplace, read through `artifacts_service`, whose path handling was
  hardened the same week (no absolute, colon, NUL, dot or dot-dot segments;
  symlink and junction components refused; containment re-checked after
  resolution). The order id is an argument the model passes, but the file
  it reaches is confined to that order's folder, and the customer scope of
  the socket that started the turn still applies.

Text is returned in bounded windows. An image or a binary file is described
-- name, kind, size -- and never read into the model's context. Hidden and
runtime-internal paths are not listed and cannot be read.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ampuly.plugins.office_ui.artifacts_service import (
    _hidden,
    _kind_for,
    artifact_path_parts,
    confined_dir,
    multiply_linked,
    read_artifact,
    redirected,
    redirected_entry,
    workspace_root_for_project,
)

# The states in which a package is a candidate rather than production.
CANDIDATE_STATUSES = frozenset({"draft", "testing"})

# A window a model can use, not a file dump.
DEFAULT_MAX_CHARS = 12_000
MAX_MAX_CHARS = 40_000
MAX_LISTED_FILES = 300
_MAX_SCAN_ENTRIES = 3_000


class MasterFileRefused(ValueError):
    """A read outside what master control was granted. `reason` is stable."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = str(reason or "").strip()


# ── candidate packages ────────────────────────────────────────────────────


def candidate_package(packages: list[Any], package_id: str) -> Any:
    """The candidate with this id, or a refusal that says why not.

    Only draft/testing packages are candidates. A published package exists
    but is not reachable from here -- production is edited through a
    candidate copy, and reading it directly would let the conversation
    describe production as if it were the thing being changed.
    """
    wanted = str(package_id or "").strip()
    if not wanted:
        raise MasterFileRefused("package_id is required", reason="package_id_required")
    for package in packages:
        if str(getattr(package, "id", "") or "") != wanted:
            continue
        status = str(getattr(package, "status", "") or "")
        if status not in CANDIDATE_STATUSES:
            raise MasterFileRefused(
                f"workflow {wanted!r} is {status or 'unknown'}, not a candidate; "
                "only draft and testing packages can be read here",
                reason="not_a_candidate",
            )
        return package
    raise MasterFileRefused(
        f"no workflow package with id {wanted!r}; call master_process_drafts "
        "or master_list_files without a package to see what exists",
        reason="unknown_package",
    )


def candidate_packages(packages: list[Any]) -> list[dict[str, Any]]:
    """The candidates on offer, by id and status. Paths are not exposed."""
    return [
        {
            "package_id": str(getattr(p, "id", "") or ""),
            "name": str(getattr(p, "name", "") or ""),
            "status": str(getattr(p, "status", "") or ""),
        }
        for p in packages
        if str(getattr(p, "status", "") or "") in CANDIDATE_STATUSES
    ]


# ── the shared confined walk ──────────────────────────────────────────────


def _list_under(root: Path) -> dict[str, Any]:
    """Files under `root`, confined, hidden ones pruned, bounded.

    A root that is itself reached through a link is refused (data phase 2, D1): a folder swapped for a junction to
    another order would otherwise be listed under this order's customer check.
    """
    if redirected(Path(root)):
        raise MasterFileRefused("the folder is reached through a link", reason="redirected")
    root = root.resolve()
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
                    if scanned > _MAX_SCAN_ENTRIES or len(files) >= MAX_LISTED_FILES:
                        truncated = True
                        break
                    absolute = Path(entry.path)
                    relative = absolute.relative_to(root)
                    if _hidden(relative) or redirected_entry(entry):
                        continue
                    try:
                        absolute.resolve().relative_to(root)
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(absolute)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        # A second name on disk can carry a file from outside this folder in (D1).
                        if multiply_linked(absolute):
                            continue
                        info = entry.stat(follow_symlinks=False)
                    except (OSError, ValueError):
                        continue
                    files.append({
                        "path": relative.as_posix(),
                        "kind": _kind_for(absolute),
                        "size": int(info.st_size),
                    })
        except OSError:
            truncated = True
    files.sort(key=lambda item: item["path"])
    return {"files": files, "truncated": truncated}


def _read_under(root: Path, relative_path: str, *, offset: int, max_chars: int) -> dict[str, Any]:
    """One file under `root`, confined the same way `read_artifact` is."""
    if redirected(Path(root)):
        raise MasterFileRefused("the folder is reached through a link", reason="redirected")
    root = root.resolve()
    parts = artifact_path_parts(relative_path)
    target = root
    for part in parts:
        target = target / part
        if redirected(target):
            raise MasterFileRefused("path contains a redirected component", reason="redirected")
    target = target.resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise MasterFileRefused("path outside the granted root", reason="outside_root") from exc
    if _hidden(relative):
        raise MasterFileRefused("path is runtime-internal", reason="hidden")
    if not target.is_file():
        raise MasterFileRefused(f"no such file: {'/'.join(parts)}", reason="not_found")
    return _describe_and_window(target, relative.as_posix(), offset=offset, max_chars=max_chars)


def _describe_and_window(target: Path, relative: str, *, offset: int, max_chars: int) -> dict[str, Any]:
    if multiply_linked(target):
        raise MasterFileRefused("the file has another name on disk", reason="multiply_linked")
    kind = _kind_for(target)
    size = int(target.stat().st_size)
    payload: dict[str, Any] = {"path": relative, "name": target.name, "kind": kind, "size": size}
    if kind != "text":
        # Described, never read. An image in a model's context is a different
        # tool with a different budget and a different approval.
        payload["content"] = None
        payload["note"] = f"{kind} file: not read into context; {size} bytes"
        return payload
    start = max(0, int(offset or 0))
    window = max(1, min(int(max_chars or DEFAULT_MAX_CHARS), MAX_MAX_CHARS))
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise MasterFileRefused("could not read file", reason="unreadable") from exc
    chunk = text[start:start + window]
    payload.update({
        "content": chunk,
        "offset": start,
        "chars": len(chunk),
        "total_chars": len(text),
        "truncated": start + len(chunk) < len(text),
    })
    return payload


# ── the two surfaces ──────────────────────────────────────────────────────


def list_candidate_files(package: Any) -> dict[str, Any]:
    root = Path(getattr(package, "root"))
    listing = _list_under(root)
    listing.update({
        "scope": "candidate",
        "package_id": str(getattr(package, "id", "") or ""),
        "status": str(getattr(package, "status", "") or ""),
    })
    return listing


def read_candidate_file(package: Any, path: str, *, offset: int = 0, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    payload = _read_under(Path(getattr(package, "root")), path, offset=offset, max_chars=max_chars)
    payload.update({"scope": "candidate", "package_id": str(getattr(package, "id", "") or "")})
    return payload


def order_root(engine: Any, project_id: str, folder_id: str) -> tuple[Path, str]:
    """`orders/<folder_id>/` in the project's workplace, and the relative prefix.

    ``folder_id`` is the order's folder: its business number (``wo-…``), or
    for a folder made before data phase 1, a task id. The caller resolves
    which (master_loop); here it is only a path segment.
    """
    folder = str(folder_id or "").strip()
    if not folder:
        raise MasterFileRefused("task_id is required", reason="task_id_required")
    # The id becomes one path segment; the same spelling rules as any other.
    (segment,) = artifact_path_parts(folder)
    workplace = workspace_root_for_project(engine, project_id)
    return workplace / "orders" / segment, f"orders/{segment}"


def _order_identity(folder_id: str, task_id: str | None, order_id: str) -> dict[str, str]:
    """What the model named (``task_id``, as passed) and the business order it resolved to (``order_id``, or "")."""
    return {"task_id": str(folder_id if task_id is None else task_id).strip(), "order_id": str(order_id or "").strip()}


def list_order_files(engine: Any, project_id: str, folder_id: str, *, task_id: str | None = None,
                     order_id: str = "") -> dict[str, Any]:
    root, prefix = order_root(engine, project_id, folder_id)
    # The order's own folder, proven to be that folder (data phase 2, D1): a junction in its place would list another
    # order's files under this order's customer check.
    try:
        confined_dir(workspace_root_for_project(engine, project_id), tuple(prefix.split("/")))
    except ValueError as exc:
        raise MasterFileRefused(str(exc), reason="redirected") from exc
    listing = _list_under(root)
    listing.update({"scope": "order", **_order_identity(folder_id, task_id, order_id), "prefix": prefix})
    return listing


def read_order_file(engine: Any, project_id: str, folder_id: str, path: str, *, offset: int = 0,
                    max_chars: int = DEFAULT_MAX_CHARS, task_id: str | None = None, order_id: str = "") -> dict[str, Any]:
    """One deliverable of one order, through the same reader the console uses.

    The path the model gives is relative to the order folder; it is joined
    to `orders/<folder_id>/` here and handed to `read_artifact`, which does
    the confinement. A path that tries to leave the order folder fails the
    spelling rules before any filesystem call.
    """
    _root, prefix = order_root(engine, project_id, folder_id)
    inner = "/".join(artifact_path_parts(path))
    full = f"{prefix}/{inner}"
    try:
        artifact = read_artifact(engine, project_id, full)
    except FileNotFoundError as exc:
        raise MasterFileRefused(f"no such deliverable: {inner}", reason="not_found") from exc
    except ValueError as exc:
        raise MasterFileRefused(str(exc), reason="refused") from exc
    target = Path(artifact["absolute_path"])
    payload = _describe_and_window(target, inner, offset=offset, max_chars=max_chars)
    payload.update({"scope": "order", **_order_identity(folder_id, task_id, order_id), "artifact_path": full})
    return payload
