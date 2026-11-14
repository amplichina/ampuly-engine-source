"""回收区: what a model "deleted" in one project, listed for the owner, put back, or cleared for good.

A model's delete never erases a file (``file_ops._recycle``, 2026-09-25): it is moved to
``<workspace root>/.ampuly-recycle/<batch>/<the file's own path from that root>``. A workspace root is the project
folder itself or one order's folder (``<project>/orders/<order>/``), so a project holds at most one recycle folder
at its root and one per order -- and those are the only places this module looks. It never walks the whole project.

Three things, all the owner's, none automatic:

* ``scan`` lists what is there, newest first, with where each file came from and when it was moved.
* ``restore`` puts one file back at its original path. Never over another file: a name that is taken is refused.
* ``clear`` deletes everything for good -- only with the token the listing handed out, which the console shows the
  owner a second time before sending. Nothing here empties the folder on its own.

Every path that leaves this module is relative to the project folder; the console never sees an absolute one.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ampuly.layer4_tools.file_ops import RECYCLE_DIR_NAME
from ampuly.plugins.office_ui.folder_move import MoveRefused

# How many files the listing shows; the count and the token cover everything up to HARD_CAP.
LIST_LIMIT = 500
HARD_CAP = 20_000
ORDERS_DIR = "orders"
BATCH = re.compile(r"^(\d{8})-(\d{6})-[0-9a-f]{6}$")


class RecycleRefused(MoveRefused):
    """Something this module will not do. ``code`` is what the console shows; never a path, never OS text.

    A ``MoveRefused`` on purpose: the 资料 entry in the handler already turns that one into a coded reply.
    """


@dataclass(frozen=True)
class Area:
    """One place a recycle folder can be: a workspace root and the folder under it."""

    key: str
    """``""`` for the project folder, ``orders/<order>`` for one order."""
    root: Path
    recycle: Path


@dataclass(frozen=True)
class Found:
    """One recycled file, as the walk saw it."""

    id: str
    original: str
    area: str
    batch: str
    moved_at: str
    size: int
    path: Path


def _same(name: str, reserved: str) -> bool:
    return os.path.normcase(str(name)) == os.path.normcase(reserved)


def _is_link(path: Path) -> bool:
    """A symlink or a Windows junction: neither is followed, neither is listed, neither is restored through."""
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return True


def areas(project_root: Path) -> list[Area]:
    """The project folder and each order folder, whether or not a recycle folder exists there yet."""
    root = Path(project_root)
    found = [Area("", root, root / RECYCLE_DIR_NAME)]
    orders = root / ORDERS_DIR
    try:
        names = sorted(entry.name for entry in os.scandir(orders)
                       if entry.is_dir(follow_symlinks=False) and not _is_link(orders / entry.name))
    except OSError:
        return found
    for name in names:
        found.append(Area(f"{ORDERS_DIR}/{name}", orders / name, orders / name / RECYCLE_DIR_NAME))
    return found


def _moved_at(batch: str, folder: Path) -> str:
    matched = BATCH.match(batch)
    if matched:
        try:
            return datetime.strptime(matched.group(1) + matched.group(2), "%Y%m%d%H%M%S").isoformat(timespec="seconds")
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(folder.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


def _walk_files(top: Path, *, budget: list[int]) -> list[tuple[Path, int]]:
    """Regular files below ``top``, links skipped, stopping when the budget runs out."""
    out: list[tuple[Path, int]] = []
    stack = [top]
    while stack and budget[0] > 0:
        folder = stack.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if budget[0] <= 0:
                break
            path = folder / entry.name
            try:
                if entry.is_symlink() or entry.is_junction():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                    continue
                if entry.is_file(follow_symlinks=False):
                    out.append((path, entry.stat(follow_symlinks=False).st_size))
                    budget[0] -= 1
            except OSError:
                continue
    return out


def _found(project_root: Path, *, cap: int = HARD_CAP) -> tuple[list[Found], bool]:
    """Everything recycled in this project, newest batch first. ``True`` when the cap cut the walk short."""
    root = Path(project_root)
    budget = [cap]
    items: list[Found] = []
    for area in areas(root):
        if not area.recycle.is_dir() or _is_link(area.recycle):
            continue
        try:
            batches = sorted((entry.name for entry in os.scandir(area.recycle)
                              if entry.is_dir(follow_symlinks=False) and not _is_link(area.recycle / entry.name)
                              and BATCH.match(entry.name)), reverse=True)
        except OSError:
            continue
        for batch in batches:
            if budget[0] <= 0:
                break
            folder = area.recycle / batch
            when = _moved_at(batch, folder)
            for path, size in _walk_files(folder, budget=budget):
                below = path.relative_to(folder)
                items.append(Found(
                    id=path.relative_to(root).as_posix(),
                    original=(area.root / below).relative_to(root).as_posix(),
                    area=area.key, batch=batch, moved_at=when, size=int(size), path=path,
                ))
    # Newest deletion first across the whole project, not area by area (the batch name is its time stamp).
    items.sort(key=lambda found: found.id)
    items.sort(key=lambda found: found.batch, reverse=True)
    return items, budget[0] <= 0


def _token(project_root: Path, items: list[Found]) -> str:
    """What the owner saw: this project, these files. Minted by the listing, checked by the clear."""
    digest = hashlib.sha256()
    digest.update(os.path.normcase(os.path.abspath(str(project_root))).encode("utf-8"))
    for item in sorted(items, key=lambda found: found.id):
        digest.update(f"|{item.id}|{item.size}".encode("utf-8"))
    return digest.hexdigest()[:20]


def scan(project_root: Path, *, limit: int = LIST_LIMIT) -> dict[str, Any]:
    """The listing the console shows: the newest ``limit`` files, the count of all of them, and the clear token."""
    items, capped = _found(project_root)
    total = sum(item.size for item in items)
    return {
        "entries": [{"id": item.id, "original": item.original, "area": item.area, "batch": item.batch,
                     "moved_at": item.moved_at, "size": item.size} for item in items[:limit]],
        "count": len(items),
        "total_bytes": total,
        "truncated": len(items) > limit,
        "capped": capped,
        "token": _token(project_root, items),
    }


def _resolve(project_root: Path, entry_id: str) -> tuple[Area, str, Path, Path]:
    """The recycled file an id names, or a refusal. Returns ``(area, batch, recycled path, original path)``."""
    text = str(entry_id or "")
    if not text or "\x00" in text or "\\" in text or text.startswith("/"):
        raise RecycleRefused("invalid_path")
    parts = PurePosixPath(text).parts
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise RecycleRefused("invalid_path")
    root = Path(project_root)
    if len(parts) >= 3 and _same(parts[0], RECYCLE_DIR_NAME):
        area = Area("", root, root / RECYCLE_DIR_NAME)
        rest = parts[1:]
    elif len(parts) >= 5 and parts[0] == ORDERS_DIR and _same(parts[2], RECYCLE_DIR_NAME):
        order_root = root / ORDERS_DIR / parts[1]
        area = Area(f"{ORDERS_DIR}/{parts[1]}", order_root, order_root / RECYCLE_DIR_NAME)
        rest = parts[3:]
    else:
        raise RecycleRefused("invalid_path")
    batch, below = rest[0], rest[1:]
    if not BATCH.match(batch) or not below:
        raise RecycleRefused("invalid_path")
    # No link anywhere on the way: a junction planted in the recycle folder must not carry a rename elsewhere.
    walk = area.root
    for part in (RECYCLE_DIR_NAME, batch, *below[:-1]):
        walk = walk / part
        if not walk.is_dir() or _is_link(walk):
            raise RecycleRefused("not_found")
    recycled = walk / below[-1]
    if _is_link(recycled) or not recycled.is_file():
        raise RecycleRefused("not_found")
    return area, batch, recycled, area.root.joinpath(*below)


def _prune(folder: Path, stop: Path) -> None:
    """Remove the now-empty folders a restore or a clear left behind, up to (not including) ``stop``."""
    current = folder
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def restore(project_root: Path, entry_id: str) -> dict[str, Any]:
    """Put one recycled file back where it came from. Refused when that place is taken; nothing is replaced."""
    root = Path(project_root)
    area, batch, recycled, original = _resolve(root, entry_id)
    if original.exists() or _is_link(original):
        raise RecycleRefused("original_exists")
    walk = area.root
    for part in original.relative_to(area.root).parts[:-1]:
        walk = walk / part
        if walk.exists() and (_is_link(walk) or not walk.is_dir()):
            raise RecycleRefused("original_exists")
    try:
        size = recycled.stat().st_size
        original.parent.mkdir(parents=True, exist_ok=True)
        os.rename(recycled, original)
    except FileExistsError as exc:
        raise RecycleRefused("original_exists") from exc
    except OSError as exc:
        raise RecycleRefused("restore_failed") from exc
    _prune(recycled.parent, area.recycle)
    return {"id": entry_id, "original": original.relative_to(root).as_posix(), "size": int(size), "area": area.key}


def clear(project_root: Path, *, token: str) -> dict[str, Any]:
    """Delete every recycled file in this project for good. Only what the owner saw and confirmed: a listing that
    has changed since (a step moved another file in) is refused with ``plan_stale`` and shown again."""
    root = Path(project_root)
    items, capped = _found(root)
    if capped:
        raise RecycleRefused("plan_incomplete")
    if not token or token != _token(root, items):
        raise RecycleRefused("plan_stale")
    removed, freed, failed = 0, 0, 0
    for item in items:
        try:
            os.remove(item.path)
        except OSError:
            failed += 1
            continue
        removed += 1
        freed += item.size
        area_root = root if not item.area else root.joinpath(*item.area.split("/"))
        _prune(item.path.parent, area_root / RECYCLE_DIR_NAME)
    if failed:
        raise RecycleRefused("clear_failed", removed=removed, failed=failed)
    return {"removed": removed, "bytes": freed}
