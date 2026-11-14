"""Pointing a project at another folder, and -- as a separate question -- importing what the old one still holds.

The owner asked for these to be two steps, and that shape is what makes the whole thing safe:

* **Pointing moves nothing.** It writes where the project looks, rewrites the roots frozen into its task rows, and
  stops. There is no window in which the files are somewhere the mapping does not know about, so there is no move
  journal and none is needed. A crash anywhere leaves every file exactly where it was.
* **Importing** is answered separately, to its own question, with the old folder's contents counted in front of the
  owner. It copies first, verifies per path, **re-walks the source and proves it unchanged**, and only then
  deletes. A source that changed while the copy ran is never deleted.

Nothing here is ever overwritten: the folder a project is pointed at has to be empty, and an import that finds a
path already present at a different size refuses before it copies anything.
"""
from __future__ import annotations

import errno
import hashlib
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from ampuly.core.project_paths import Mapping, mapping as read_mapping, save as save_mapping
from ampuly.core.sqlite_open import opened
from ampuly.database.store import default_runtime_db_path
from ampuly.plugins.office_ui.artifacts_service import redirected, redirected_entry

PLAN_TTL_SEC = 600
PLAN_ENTRIES = 200_000
PLAN_SECONDS = 5.0
PROGRESS_SECONDS = 2.0
PROMOTE_RETRIES = 6
STAGING_PREFIX = ".ampuly-import-"
# What the workplace itself holds. Attachments live in the system directory and never move, so an import total is
# legitimately smaller than 资料's 占用 -- the console says so rather than looking like a miscount.
BUCKETS = ("orders", "delivery-history", "exports", "project-materials", "other", "internal")
RUNNING_ORDER_STATUSES = frozenset({"launching", "launched", "producing", "delivering"})
_STICKY_KEYS = ("workspace_root", "comms_workspace_root", "output_root", "target_output_dir", "comms_root")


class MoveRefused(Exception):
    """Something this module will not do. ``code`` is what the console shows; never a path, never OS text."""

    def __init__(self, code: str, **facts: Any) -> None:
        super().__init__(code)
        self.code = str(code or "import_failed")
        self.facts = facts


@dataclass(frozen=True)
class Plan:
    """What the old folder holds, and a token that says "this exact tree, a moment ago"."""

    source: str = ""
    target: str = ""
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    buckets: dict[str, dict[str, int]] = field(default_factory=dict)
    entries: dict[str, int] = field(default_factory=dict)
    complete: bool = True
    same_volume: bool = True
    token: str = ""
    made_at: float = 0.0

    def as_payload(self) -> dict[str, Any]:
        """What the console shows. The per-path map stays on the server: it is for verifying, not for reading."""
        return {"source": self.source, "target": self.target, "files": self.files, "dirs": self.dirs,
                "bytes": self.bytes, "buckets": self.buckets, "complete": self.complete,
                "same_volume": self.same_volume, "token": self.token}


@dataclass(frozen=True)
class PointResult:
    source: str = ""
    target: str = ""
    rewritten: dict[str, int] = field(default_factory=dict)
    legacy: dict[str, Any] | None = None


@dataclass(frozen=True)
class ImportResult:
    files: int = 0
    bytes: int = 0
    elapsed_ms: int = 0
    skipped: int = 0
    leftovers: dict[str, Any] | None = None


# ── Is anything running? ─────────────────────────────────────────────────────────────────────────────────────────

def busy_reason(business: Any, ampuly_home: Path, project_id: str) -> str:
    """Why neither step may run right now, or ``""``.

    A running task holds a pinned directory descriptor on the folder it started with, so it would keep writing to
    the old place while a fresh lookup answered the new one. Both steps refuse, not only the import.
    """
    project = str(project_id or "").strip()
    try:
        # Asked per running status, not read off the newest 500: a project past 500 orders with an older one still
        # running was judged idle and the move let through (2026-09-25). The store filters by status itself; the
        # check on what comes back stays, so a store that ignores the filter cannot answer "idle" by accident.
        for status in sorted(RUNNING_ORDER_STATUSES):
            for order in business.list_orders(project_id=project, status=status, limit=1) or []:
                if str(getattr(order, "status", "") or "") in RUNNING_ORDER_STATUSES:
                    return "order_running"
    except Exception:  # noqa: BLE001 - a store that cannot be read is not proof that nothing runs
        return "project_busy"
    # 默认项目的运行库可能还在老位置（搬迁前）或已经在项目文件夹里（搬迁后）——问那个答案，别写死路径。
    tasks_db = (default_runtime_db_path(ampuly_home) if project == "default"
                else Path(ampuly_home) / "projects" / project / "tasks.db")
    if not tasks_db.is_file():
        return ""
    try:
        # 用完真的关掉。以前这里是 `with sqlite3.connect(...)`，那只提交不关闭：漏下的句柄在 Windows 上
        # 把运行库锁住，于是紧接着的删除删不掉它——而删除用的是 ignore_errors，失败不出声。
        with opened(tasks_db, read_only=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE status IN ('running', 'pending') LIMIT 1").fetchone()
        return "project_busy" if row else ""
    except sqlite3.Error:
        return ""


# ── What is there ────────────────────────────────────────────────────────────────────────────────────────────────

def _bucket_of(relative: str) -> str:
    head = relative.split("/", 1)[0]
    if head.startswith("."):
        return "internal"
    if head in {"orders", "delivery-history", "project-materials"}:
        return head
    if head in {"delivery-exports", "portfolio-exports"}:
        return "exports"
    return "other"


def plan(source: Path, target: Path, *, project_id: str = "", entries_cap: int = PLAN_ENTRIES,
         seconds: float = PLAN_SECONDS) -> Plan:
    """Walk the old folder once and say exactly what is in it. Reads only; creates nothing.

    A link anywhere in the tree, or a file with a second name on disk, refuses the whole plan: an import must not
    carry in something that is not the project's, and must not delete a name that another name still points at.
    """
    root = Path(source)
    started = time.monotonic()
    files = dirs = total = 0
    newest = 0
    entries: dict[str, int] = {}
    buckets = {name: {"bytes": 0, "files": 0} for name in BUCKETS}
    complete = True
    pending = [root]
    seen = 0
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as scan:
                for entry in scan:
                    seen += 1
                    if seen > entries_cap or (time.monotonic() - started) > seconds:
                        complete = False
                        pending = []
                        break
                    if redirected_entry(entry):
                        raise MoveRefused("source_has_link")
                    relative = Path(entry.path).relative_to(root).as_posix()
                    if entry.is_dir(follow_symlinks=False):
                        dirs += 1
                        pending.append(Path(entry.path))
                        continue
                    try:
                        info = os.stat(entry.path, follow_symlinks=False)
                    except OSError:
                        raise MoveRefused("source_unreadable") from None
                    if int(info.st_nlink) != 1:
                        raise MoveRefused("source_multiply_linked")
                    files += 1
                    total += int(info.st_size)
                    newest = max(newest, int(info.st_mtime_ns))
                    entries[relative] = int(info.st_size)
                    bucket = buckets[_bucket_of(relative)]
                    bucket["bytes"] += int(info.st_size)
                    bucket["files"] += 1
        except FileNotFoundError:
            continue
        except PermissionError:
            raise MoveRefused("source_unreadable") from None
    digest = hashlib.sha256("|".join([
        str(project_id), _real(root), _real(Path(target)), str(files), str(dirs), str(total), str(newest),
    ]).encode("utf-8")).hexdigest()[:20]
    return Plan(source=str(root), target=str(target), files=files, dirs=dirs, bytes=total, buckets=buckets,
                entries=entries, complete=complete,
                same_volume=Path(root).anchor.lower() == Path(target).anchor.lower(),
                token=digest, made_at=time.time())


def _real(path: Path) -> str:
    try:
        return os.path.normcase(os.path.realpath(str(path)))
    except OSError:
        return os.path.normcase(os.path.abspath(str(path)))


def legacy_summary(source: Path, target: Path, *, project_id: str = "") -> dict[str, Any] | None:
    """What the old folder still holds, as the body of the question the console asks next. ``None`` when empty."""
    root = Path(source)
    if not root.is_dir():
        return None
    try:
        found = plan(root, Path(target), project_id=project_id)
    except MoveRefused as refused:
        return {"path": str(root), "files": 0, "bytes": 0, "buckets": {}, "complete": False,
                "token": "", "refused": refused.code}
    if found.files == 0 and found.dirs == 0:
        return None
    payload = found.as_payload()
    payload["path"] = str(root)
    return payload


# ── Step one: pointing ───────────────────────────────────────────────────────────────────────────────────────────

def point(home: Path, project_id: str, target: Path | None, *, current: Path, actor: str = "owner") -> PointResult:
    """Write where the project looks, move nothing, and report what the old folder still holds.

    ``target`` of ``None`` means 「回到默认根目录」: the pin is removed and the project resolves by arithmetic again.
    """
    project = str(project_id or "").strip()
    found = read_mapping(Path(home))
    projects = dict(found.projects)
    if target is None:
        projects.pop(project, None)
    else:
        projects[project] = Path(target)
    if target is not None:
        # The chooser normally creates it, but a folder chosen and then removed, or one whose check created and
        # tidied it away, must still exist afterwards: from here on a pinned folder that is missing is refused,
        # never manufactured.
        Path(target).mkdir(parents=True, exist_ok=True)
    legacy = dict(found.legacy)
    still_there = Path(current).is_dir() and any(Path(current).iterdir())
    if still_there:
        legacy[project] = Path(current)
    else:
        legacy.pop(project, None)
    after = save_mapping(Path(home), default_root=found.default_root, projects=projects, legacy=legacy, actor=actor)
    if target is not None and after.pinned(project) is None:
        # The file was written and then ignored on the way back in: the worst outcome available here.
        save_mapping(Path(home), default_root=found.default_root, projects=dict(found.projects),
                     legacy=dict(found.legacy), actor=actor)
        raise MoveRefused("pin_rejected")
    from ampuly.core.config import get_project_workplace

    landed = Path(get_project_workplace(project))
    rewritten = rewrite_workspace_roots(Path(home) / "projects" / project / "tasks.db", Path(current), landed)
    return PointResult(source=str(current), target=str(landed), rewritten=rewritten,
                       legacy=legacy_summary(Path(current), landed, project_id=project))


def forget_legacy(home: Path, project_id: str, *, actor: str = "owner") -> None:
    """The old folder is dealt with: stop offering to import from it."""
    found = read_mapping(Path(home))
    if str(project_id) not in found.legacy:
        return
    legacy = {pid: path for pid, path in found.legacy.items() if pid != str(project_id)}
    save_mapping(Path(home), default_root=found.default_root, projects=dict(found.projects),
                 legacy=legacy, actor=actor)


def rewrite_workspace_roots(tasks_db: Path, old: Path, new: Path) -> dict[str, int]:
    """Repoint the absolute roots frozen into this project's task rows.

    A task's workspace root is written into its metadata when it starts and wins over a fresh lookup for ever
    after. Left alone, a project that moved would keep writing to where it used to be.
    """
    import json

    counts: dict[str, int] = {}
    path = Path(tasks_db)
    if not path.is_file():
        return counts
    old_key = os.path.normcase(str(old))
    try:
        with opened(path, row_factory=sqlite3.Row) as conn:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for table in tables:
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                column = "metadata_json" if "metadata_json" in columns else (
                    "metadata" if "metadata" in columns else "")
                if not column or "id" not in columns:
                    continue
                changed = 0
                for row in conn.execute(f"SELECT id, {column} FROM {table}").fetchall():
                    raw = row[column]
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    if _repoint(data, old_key, str(new)):
                        conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?",
                                     (json.dumps(data, ensure_ascii=False), row["id"]))
                        changed += 1
                if changed:
                    counts[table] = changed
            conn.commit()
    except sqlite3.Error as exc:
        logger.warning(f"project folder: task roots not rewritten ({type(exc).__name__})")
    return counts


def _repoint(data: dict, old_key: str, new_root: str) -> bool:
    """Replace every frozen root that starts with the old folder, at the top level and one level in."""
    touched = False
    holders = [data]
    for nested in ("execution_defaults", "_execution_context"):
        inner = data.get(nested)
        if isinstance(inner, dict):
            holders.append(inner)
    for holder in holders:
        for key in _STICKY_KEYS:
            value = holder.get(key)
            if not isinstance(value, str) or not value:
                continue
            normalised = os.path.normcase(value)
            if normalised == old_key:
                holder[key] = new_root
                touched = True
            elif normalised.startswith(old_key.rstrip("\\/") + os.sep):
                holder[key] = str(Path(new_root) / Path(value[len(str(old_key)):].lstrip("\\/")))
                touched = True
    return touched


# ── Step two: importing ──────────────────────────────────────────────────────────────────────────────────────────

def import_legacy(source: Path, target: Path, *, confirmed: str, project_id: str = "",
                  on_progress: Callable[[int, int], None] | None = None) -> ImportResult:
    """Move what the old folder still holds into the project's folder, and only then delete the old one."""
    started = time.monotonic()
    old, new = Path(source), Path(target)
    if _real(old) == _real(new):
        raise MoveRefused("same_folder")
    if not new.is_dir():
        raise MoveRefused("target_missing")
    fresh = plan(old, new, project_id=project_id, entries_cap=PLAN_ENTRIES, seconds=PLAN_SECONDS)
    if not fresh.complete:
        raise MoveRefused("plan_incomplete")
    if confirmed and fresh.token != confirmed:
        raise MoveRefused("plan_stale")
    if time.time() - fresh.made_at > PLAN_TTL_SEC:
        raise MoveRefused("plan_stale")

    # Nothing is ever overwritten. A path already there at the same size is what an interrupted import left.
    conflicts = 0
    skipped: set[str] = set()
    for relative, size in fresh.entries.items():
        landing = new / relative
        if not landing.exists():
            continue
        try:
            if landing.is_file() and landing.stat().st_size == size:
                skipped.add(relative)
                continue
        except OSError:
            pass
        conflicts += 1
    if conflicts:
        raise MoveRefused("import_conflict", conflicts=conflicts)

    staging = new.parent / f"{STAGING_PREFIX}{uuid.uuid4().hex}"
    moved_bytes = 0
    last_beat = time.monotonic()
    try:
        shutil.copytree(old, staging, symlinks=True, dirs_exist_ok=False)
        for relative, size in fresh.entries.items():
            moved_bytes += size
            if on_progress is not None and time.monotonic() - last_beat >= PROGRESS_SECONDS:
                on_progress(moved_bytes, fresh.bytes)
                last_beat = time.monotonic()
    except MoveRefused:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 - a copy that failed leaves both sides as they were
        shutil.rmtree(staging, ignore_errors=True)
        logger.warning(f"project folder: import copy failed ({type(exc).__name__}: {exc})")
        raise MoveRefused("import_failed") from exc

    # Verify per path, not by totals: three numbers cannot tell a correct tree from one whose files were swapped.
    try:
        copied = _walk_sizes(staging)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise MoveRefused("verify_failed") from exc
    if copied != fresh.entries:
        shutil.rmtree(staging, ignore_errors=True)
        raise MoveRefused("verify_failed")

    # The delete is gated on the source's own present state, never on a plan taken earlier.
    try:
        again = plan(old, new, project_id=project_id, entries_cap=PLAN_ENTRIES, seconds=PLAN_SECONDS)
    except MoveRefused:
        again = None
    if again is None or again.token != fresh.token:
        _merge(staging, new, fresh.entries, skipped)
        shutil.rmtree(staging, ignore_errors=True)
        raise MoveRefused("source_changed", files=fresh.files)

    _merge(staging, new, fresh.entries, skipped)
    shutil.rmtree(staging, ignore_errors=True)

    leftovers = None
    try:
        shutil.rmtree(old, ignore_errors=False)
    except OSError:
        remaining = sum(1 for _ in old.rglob("*")) if old.exists() else 0
        leftovers = {"path": str(old), "entries": remaining}
    return ImportResult(files=fresh.files, bytes=fresh.bytes,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        skipped=len(skipped), leftovers=leftovers)


def _walk_sizes(root: Path) -> dict[str, int]:
    """Every file under ``root`` by relative path and size."""
    found: dict[str, int] = {}
    pending = [Path(root)]
    while pending:
        current = pending.pop()
        with os.scandir(current) as scan:
            for entry in scan:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    continue
                relative = Path(entry.path).relative_to(root).as_posix()
                found[relative] = int(os.stat(entry.path, follow_symlinks=False).st_size)
    return found


def _merge(staging: Path, target: Path, entries: dict[str, int], skipped: set[str]) -> None:
    """Put the verified copy in place, one path at a time, never over something already there."""
    for relative in entries:
        if relative in skipped:
            continue
        source = Path(staging) / relative
        landing = Path(target) / relative
        landing.parent.mkdir(parents=True, exist_ok=True)
        if landing.exists():
            continue
        promote(source, landing)


def promote(staged: Path, target: Path) -> None:
    """Put a staged path where it belongs: a rename when it can be, a copy across volumes, retried on a lock.

    Windows raises PermissionError when something else still holds the name for a moment -- an antivirus scan, an
    Explorer window. The same shape the project service has used for its own staging since 2026-09.
    """
    source, landing = Path(staged), Path(target)
    for attempt in range(PROMOTE_RETRIES):
        try:
            source.rename(landing)
            return
        except PermissionError:
            if attempt == PROMOTE_RETRIES - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.EXDEV or getattr(exc, "winerror", None) == 17:
                shutil.move(str(source), str(landing))
                return
            raise


# ── The shared root ──────────────────────────────────────────────────────────────────────────────────────────────

def set_default_root(home: Path, new_root: Path, *, actor: str = "owner") -> Mapping:  # noqa: D401
    """Change where new projects go, and keep every existing project exactly where it is.

    Every project that has a folder today is pinned to that folder in the same write, so 「改它不会搬动任何现有
    项目」 is a property of the file rather than a hope.
    """
    from ampuly.core.config import get_default_workplace_root

    found = read_mapping(Path(home))
    old_root = Path(get_default_workplace_root())
    pins = dict(found.projects)
    for candidate in _project_ids(Path(home), old_root):
        if candidate in pins:
            continue
        folder = old_root / candidate
        if folder.is_dir() and not redirected(folder):
            pins[candidate] = folder
    before = read_mapping(Path(home))
    after = save_mapping(Path(home), default_root=Path(new_root), projects=pins,
                         legacy=dict(before.legacy), actor=actor)
    if after.dropped:
        # A written-but-ignored pin is the worst outcome available: the project would resolve under the new root,
        # where nothing of its own has ever been.
        save_mapping(Path(home), default_root=before.default_root, projects=dict(before.projects),
                     legacy=dict(before.legacy), actor=actor)
        raise MoveRefused("pin_rejected", dropped=len(after.dropped))
    return after


def _project_ids(home: Path, root: Path) -> list[str]:
    """Every project this console knows: one with a folder under the shared root, or a home directory of its own."""
    from ampuly.project_id import is_valid_project_id

    found: set[str] = set()
    for base in (Path(root), Path(home) / "projects"):
        try:
            with os.scandir(base) as scan:
                for entry in scan:
                    if entry.is_dir(follow_symlinks=False) and is_valid_project_id(entry.name):
                        found.add(entry.name)
        except OSError:
            continue
    return sorted(found)
