"""What a turn actually left behind in the workspace.

A CLI Provider writes files and says nothing about them. The turn returns
text, the files land in the workspace, and "what did this run produce" is
answered by a person looking in a folder and guessing which things are new.
That guess is the reason a delivery gets shipped with last week's draft in it.

So: take stock before the turn, take stock after, and report the difference.
Created, modified and deleted are kept apart, because they are different news
-- a file the turn overwrote was somebody's work a moment ago.

Two rules make the answer trustworthy rather than merely plausible:

**Bounded.** A scan walks a delivery folder, not a disk. It skips the places
that are large and never the point (``.git``, ``node_modules``, virtualenvs,
caches), stops at a depth and a file count, and refuses to follow a symlink
out of the workspace. An unbounded scan on somebody's machine is not a
feature.

**Honest when it stops early.** Hitting the limit sets ``truncated``. A
partial list that presents itself as complete is worse than no list: the
missing file is the one nobody looks for.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Large, generated, or none of the caller's business. Named rather than
# pattern-matched so that adding one is a decision somebody can read.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode",
    "dist", "build", ".next", ".turbo", ".cache", ".tmp", ".backups",
})
MAX_FILES = 5000
MAX_DEPTH = 12


@dataclass(frozen=True)
class FileStamp:
    """Enough to tell "the same file" from "the file, changed"."""

    size: int
    mtime_ns: int


@dataclass
class WorkspaceSnapshot:
    """The workspace as it stood at one moment, within the stated bounds."""

    root: str
    files: dict[str, FileStamp] = field(default_factory=dict)
    truncated: bool = False
    unreadable: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.files)

    @property
    def complete(self) -> bool:
        """Complete within configured exclusions, not a forensic disk snapshot."""
        return not self.truncated and not self.unreadable


def snapshot_workspace(
    root: str | Path,
    *,
    max_files: int = MAX_FILES,
    max_depth: int = MAX_DEPTH,
    skip_dirs: Iterable[str] = SKIP_DIRS,
) -> WorkspaceSnapshot:
    """Stock-take of one workspace, bounded and never leaving it."""
    base = Path(str(root or "")).resolve()
    snapshot = WorkspaceSnapshot(root=str(base))
    if not base.is_dir():
        snapshot.unreadable.append(".")
        return snapshot
    skip = {str(name) for name in skip_dirs}
    def walk_error(error: OSError) -> None:
        snapshot.unreadable.append(_relative(Path(error.filename or base), base))

    for current, dirnames, filenames in os.walk(base, followlinks=False, onerror=walk_error):
        here = Path(current)
        try:
            depth = len(here.resolve().relative_to(base).parts)
        except ValueError:
            # A symlinked directory pointing outside: not part of this
            # workspace, and not ours to walk.
            dirnames[:] = []
            continue
        children = []
        for name in sorted(dirnames):
            candidate = here / name
            if name in skip:
                continue
            try:
                linked = candidate.is_symlink() or bool(getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400)
                if not linked:
                    children.append(name)
            except OSError:
                snapshot.unreadable.append(_relative(candidate, base))
        if depth >= max_depth:
            if children:
                snapshot.truncated = True
            dirnames[:] = []
        else:
            dirnames[:] = children
        for name in sorted(filenames):
            if len(snapshot.files) >= max_files:
                snapshot.truncated = True
                return snapshot
            path = here / name
            try:
                if path.is_symlink():
                    # A link is not a produced file; following one can leave
                    # the workspace entirely.
                    continue
                stat = path.stat()
            except OSError:
                snapshot.unreadable.append(_relative(path, base))
                continue
            snapshot.files[_relative(path, base)] = FileStamp(stat.st_size, stat.st_mtime_ns)
    return snapshot


def _relative(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base)).replace(os.sep, "/")
    except (ValueError, OSError):
        return str(path).replace(os.sep, "/")


# A baseline is kept with the turn so that an INTERRUPTED turn can still be compared against the workspace later. It
# is a row in a database, so it is bounded: past this many files no baseline is kept, and the reader is told so.
MAX_BASELINE_FILES = 2000
MAX_RECORDED_CHANGES = 200


def baseline_record(snapshot: WorkspaceSnapshot | None) -> dict[str, Any]:
    """The snapshot as a bounded, storable record -- or the reason there is none."""
    if snapshot is None:
        return {"kept": False, "reason": "the workspace could not be scanned before the turn"}
    if snapshot.count > MAX_BASELINE_FILES:
        return {"kept": False, "reason": f"the workspace has {snapshot.count} files; a baseline is kept up to {MAX_BASELINE_FILES}",
                "root": snapshot.root, "count": snapshot.count}
    return {"kept": True, "root": snapshot.root, "count": snapshot.count, "complete": snapshot.complete,
            "files": {path: [stamp.size, stamp.mtime_ns] for path, stamp in snapshot.files.items()}}


def snapshot_from_record(record: dict[str, Any]) -> WorkspaceSnapshot | None:
    if not isinstance(record, dict) or not record.get("kept"):
        return None
    files = {str(path): FileStamp(int(pair[0]), int(pair[1])) for path, pair in dict(record.get("files", {}) or {}).items()
             if isinstance(pair, (list, tuple)) and len(pair) == 2}
    return WorkspaceSnapshot(root=str(record.get("root", "") or ""), files=files, truncated=not bool(record.get("complete", True)))


def changes_record(changes: list[dict[str, Any]], *, scan_complete: bool) -> dict[str, Any]:
    """Paths and what happened to them, never contents; cut at a bound, and saying so."""
    listed = [{"path": str(item.get("path", ""))[:300], "change": str(item.get("change", ""))} for item in changes[:MAX_RECORDED_CHANGES]]
    return {"changes": listed, "total": len(changes), "changes_truncated": len(changes) > MAX_RECORDED_CHANGES,
            "scan_complete": bool(scan_complete)}


def diff_snapshots(before: WorkspaceSnapshot, after: WorkspaceSnapshot) -> list[dict[str, Any]]:
    """What the turn did, one entry per file, sorted so runs compare cleanly.

    ``modified`` compares size and modification time. A write that somehow
    reproduced both would read as unchanged; every real editor and every
    Provider CLI updates the timestamp, so this is a theoretical hole rather
    than a practical one -- named here so nobody has to rediscover it.
    """
    if before.root != after.root:
        raise ValueError("cannot compare snapshots of different workspaces")
    changes: list[dict[str, Any]] = []
    for path, stamp in after.files.items():
        previous = before.files.get(path)
        if previous is None:
            # Missing from a partial baseline does not prove a newly made file.
            changes.append({"path": path, "change": "created" if before.complete else "observed",
                            "size": stamp.size})
        elif previous != stamp:
            changes.append({
                "path": path, "change": "modified", "size": stamp.size,
                "size_before": previous.size,
            })
    for path, stamp in before.files.items():
        if path not in after.files and after.complete:
            changes.append({"path": path, "change": "deleted", "size_before": stamp.size})
    changes.sort(key=lambda entry: (entry["change"], entry["path"]))
    return changes


def collect_turn_artifacts(
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
    *,
    session_id: str = "",
    turn_id: str = "",
) -> list[dict[str, Any]]:
    """The diff as artifact records, carrying the bound that produced them.

    Every record says whether the scan it came from was complete. A caller
    that renders "3 files produced" from a truncated scan is stating something
    nobody established.
    """
    truncated = not (before.complete and after.complete)
    artifacts: list[dict[str, Any]] = []
    for entry in diff_snapshots(before, after):
        artifacts.append({
            **entry,
            "kind": "workspace_file",
            "session_id": str(session_id or ""),
            "turn_id": str(turn_id or ""),
            "scan_complete": not truncated,
        })
    return artifacts
