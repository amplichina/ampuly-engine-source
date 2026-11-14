"""Move a legacy ``.opc`` / ``OPC_HOME`` data directory to ``.ampuly`` / ``AMPULY_HOME``.

Dry-run is the default and writes nothing. ``--apply`` copies only after the
engine is stopped: a SQLite backup of the source, a whole-target move-aside
when the destination is an empty shadow (or ``--replace-target``), then a
hash and row-count check. The source directory is never modified or deleted.
``--rollback`` parks the migrated tree and puts a moved-aside shadow back.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import re
import shutil
import sqlite3
import stat
import sys
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

RUNTIME_MARKERS = ("business.db", "global.db", "tasks.db", "logs", "workplace", "memory")
LEGACY_DIR_NAME = ".opc"
CURRENT_DIR_NAME = ".ampuly"
_DIR_ALIASES = (
    (".openopc", ".ampuly"),
    (".opc-comms", ".ampuly-comms"),
)
_LEGACY_COLUMNS = (
    ("agents", "opc_role_id", "ampuly_role_id"),
    ("custom_agents_shadow", "opc_role_id", "ampuly_role_id"),
    ("external_sessions", "opc_session_id", "ampuly_session_id"),
    ("external_sessions_v2", "opc_session_id", "ampuly_session_id"),
)
_NOTE_ONCE: set[str] = set()
_SHADOW_CACHE: dict[str, tuple[object, bool]] = {}
RECEIPT_NAME = "home-migration.json"
ROLLBACK_NAME = "ROLLBACK.txt"
RECEIPT_VERSION = 1
_REPARSE = 0x400
_CANONICAL_COLUMN = {old: new for _table, old, new in _LEGACY_COLUMNS}
_ENV_LINE = re.compile(r"^(?P<export>export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")
# Top-level names a first-start shadow may hold besides empty databases.
_SHADOW_TOP = frozenset({
    "config", "skills", "prompts", "logs", "workflows", "browsers",
    "node_trials", "plugins", "projects",
    "office_ui.lock", "project_paths.json", "accounts.json",
    RECEIPT_NAME, ROLLBACK_NAME,
})
# User material. An empty database does not make these disposable.
_USER_MATERIAL = frozenset({
    "workplace", "memory", "attachments", "exports", "backups",
    "customer_service", "browser",
})


class _ApplyFailure(Exception):
    """Apply started work and must roll the destination back. The source stays."""


# ``strftime("%Y%m%d-%H%M%S")`` is this wide. Dry-run uses it so the reported
# backup path has the same length apply will write.
_STAMP_PLACEHOLDER = "YYYYMMDD-HHMMSS"
if len(_STAMP_PLACEHOLDER) != len(datetime(2000, 1, 2, 3, 4, 5).strftime("%Y%m%d-%H%M%S")):
    raise RuntimeError("backup stamp placeholder must match the apply timestamp width")


def extended_io_path(path: str | Path, *, platform: str | None = None) -> str:
    """Path string for filesystem and SQLite calls.

    POSIX returns the path unchanged. Windows returns an absolute ``\\\\?\\``
    or ``\\\\?\\UNC\\`` path so a name past MAX_PATH still opens. The prefix
    is not applied twice. ``.`` and ``..`` are normalized first, because
    ``\\\\?\\`` would otherwise treat them as literal directory names.
    """
    system = os.name if platform is None else platform
    text = os.fspath(path)
    if system != "nt":
        return text
    if text.startswith("\\\\?\\"):
        return text.replace("/", "\\")
    text = text.replace("/", "\\")
    if not (text.startswith("\\\\") or (len(text) >= 3 and text[1] == ":" and text[2] == "\\")):
        if platform not in (None, os.name):
            raise ValueError("a relative Windows path cannot be prefixed off Windows")
        text = os.path.abspath(text)
        return extended_io_path(text, platform=system)
    text = ntpath.normpath(text)
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _strip_extended(text: str) -> str:
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[len("\\\\?\\UNC\\"):]
    if text.startswith("\\\\?\\"):
        return text[len("\\\\?\\"):]
    return text


def io_path(path: str | Path) -> str:
    """Path to pass to a syscall or to SQLite.

    POSIX is unchanged. On Windows a path that fits in MAX_PATH stays plain:
    ``\\\\?\\`` and the plain spelling are different SQLite identities, so a
    WAL created under the plain path must be opened the same way or its
    ``-shm`` is remapped. Paths that do not fit use :func:`extended_io_path`.
    """
    if os.name != "nt":
        return os.fspath(path)
    extended = extended_io_path(path)
    plain = _strip_extended(extended)
    if len(plain) >= 260:
        return extended
    return plain


def _lstat(path: Path):
    return os.lstat(io_path(path))


def _exists(path: Path) -> bool:
    try:
        _lstat(path)
    except OSError:
        return False
    return True


def _is_dir_entry(path: Path) -> bool:
    """A real directory. Links and junctions are not directories to walk into."""
    try:
        st = _lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE):
        return False
    return stat.S_ISDIR(st.st_mode)


def _is_file_entry(path: Path) -> bool:
    try:
        st = _lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE):
        return False
    return stat.S_ISREG(st.st_mode)


def _mkdir(path: Path) -> None:
    # Windows directory creation reaches its legacy limit before file opens.
    # Prefix filesystem paths here; keep SQLite's separate identity policy.
    os.makedirs(extended_io_path(path), exist_ok=True)


def _read_bytes(path: Path) -> bytes:
    with open(io_path(path), "rb") as handle:
        return handle.read()


def _write_bytes(path: Path, data: bytes) -> None:
    _mkdir(path.parent)
    with open(io_path(path), "wb") as handle:
        handle.write(data)


def _write_text(path: Path, text: str) -> None:
    """Same newline translation as ``Path.write_text``."""
    _mkdir(path.parent)
    with open(io_path(path), "w", encoding="utf-8") as handle:
        handle.write(text)


def _read_text(path: Path) -> str:
    with open(io_path(path), "r", encoding="utf-8") as handle:
        return handle.read()


def _unlink(path: Path) -> None:
    os.unlink(io_path(path))


def _remove_link(path: Path) -> None:
    """Remove a symlink or junction itself. Do not delete the directory it points at."""
    io = io_path(path)
    try:
        os.unlink(io)
    except OSError:
        os.rmdir(io)


def _rename(src: Path, dst: Path) -> None:
    os.rename(io_path(src), io_path(dst))


def _copy_file(src: Path, dst: Path) -> None:
    _mkdir(dst.parent)
    shutil.copy2(io_path(src), io_path(dst), follow_symlinks=False)


def _connect_rw(path: Path, *, timeout: float) -> sqlite3.Connection:
    """Writable SQLite open. Windows uses :func:`io_path` so WAL locking matches."""
    if os.name != "nt":
        return sqlite3.connect(str(path), timeout=timeout)
    return sqlite3.connect(io_path(path), timeout=timeout)


def _walk(root: Path):
    """``os.walk`` that does not follow links and can list past MAX_PATH.

    Yields directories joined back onto ``root``, so ``relative_to(root)``
    keeps working after the Windows prefix is stripped.
    """
    if not _is_dir_entry(root):
        return
    # Always enumerate through the extended prefix on Windows. A short root can
    # still contain a child past MAX_PATH; walking the plain root stops there.
    start = extended_io_path(root) if os.name == "nt" else os.fspath(root)
    anchor = _strip_extended(start) if os.name == "nt" else start
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        here = _strip_extended(dirpath) if os.name == "nt" else dirpath
        if os.name == "nt":
            rel = ntpath.relpath(here, anchor)
            logical = root if rel in {".", ""} else root.joinpath(*Path(rel).parts)
        else:
            logical = Path(dirpath)
        yield logical, dirnames, filenames


def _remove_tree(root: Path) -> None:
    """Delete a tree we created. Junctions and symlinks are removed, not followed."""
    if _is_link_or_junction(root):
        raise OSError(f"refuse to remove a link: {root}")
    directories: list[Path] = []
    for current, dirnames, filenames in _walk(root):
        for name in list(dirnames):
            child = current / name
            if _is_link_or_junction(child):
                dirnames.remove(name)
                _remove_link(child)
        for name in filenames:
            child = current / name
            if _is_link_or_junction(child):
                _remove_link(child)
            else:
                _unlink(child)
        directories.append(current)
    for directory in reversed(directories):
        os.rmdir(io_path(directory))


def note_once(key: str, message: str) -> None:
    """Print a deprecation line once per process."""
    if key in _NOTE_ONCE:
        return
    _NOTE_ONCE.add(key)
    print(message, file=sys.stderr, flush=True)


def has_runtime(path: Path) -> bool:
    if not path.is_dir():
        return False
    if any((path / name).exists() for name in RUNTIME_MARKERS):
        return True
    return any(path.glob("*.db"))


def _is_link_or_junction(path: Path) -> bool:
    """True for symlinks and Windows junctions/mount points that must not be followed.

    ``S_ISLNK`` and ``FILE_ATTRIBUTE_REPARSE_POINT`` are checked before any
    regular-file short-circuit: on Windows a file can be ``S_ISREG`` and still
    be a reparse point. A stat failure is not "not a link". Missing paths are
    the only exception. The tree walk uses directory-entry attributes instead
    of this call; see :func:`_find_reparse`.
    """
    try:
        st = _lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    if bool(getattr(st, "st_file_attributes", 0) & _REPARSE):
        return True
    return False


def _ancestor_reparse(path: Path) -> Path | None:
    for parent in path.parents:
        if _is_link_or_junction(parent):
            return parent
    return None


def _is_sqlite_db(name: str) -> bool:
    return name.endswith((".db", ".sqlite", ".sqlite3")) and not _is_sqlite_sidecar(name)


def _is_sqlite_sidecar(name: str) -> bool:
    return name.endswith(("-wal", "-shm", "-journal")) and (
        ".db" in name or ".sqlite" in name
    )


def _is_env_file(path: Path) -> bool:
    return path.name == ".env" or path.name.startswith(".env.")


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(io_path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ro_connect(path: Path) -> sqlite3.Connection:
    """Read-only SQLite open. Never a read-write handle on a database that already has a WAL.

    A read-write open rewrites ``-shm``. On Windows that rewrite is a different
    mapping when the filename is ``\\\\?\\`` rather than the plain path that
    created the WAL, and the source bytes then no longer match.
    """
    if os.name != "nt":
        return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    filename = io_path(path)
    if filename.startswith("\\\\?\\"):
        quoted = urllib.parse.quote(filename, safe="/:")
        return sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=5.0)
    return sqlite3.connect(Path(filename).resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)


def _absolute_key(path: Path) -> str:
    """Comparable absolute path. Windows does not follow links and ignores MAX_PATH."""
    if os.name != "nt":
        return str(path.resolve())
    return ntpath.normcase(_strip_extended(extended_io_path(path)))


def _paths_nested(a: Path, b: Path) -> bool:
    """True if either resolved path is strictly inside the other."""
    try:
        if os.name == "nt":
            ar = _absolute_key(a)
            br = _absolute_key(b)
            if ar == br:
                return False
            return ar.startswith(br + "\\") or br.startswith(ar + "\\")
        ar = a.resolve()
        br = b.resolve()
    except OSError:
        return True
    if ar == br:
        return False
    try:
        ar.relative_to(br)
        return True
    except ValueError:
        pass
    try:
        br.relative_to(ar)
        return True
    except ValueError:
        return False


def resolve_unconfigured_home(project_root: Path) -> Path:
    """Pick the data directory when neither AMPULY_HOME nor a legacy variable is set.

    A checkout that already contains the tracked ``.ampuly`` templates must
    still read a live ``.opc`` until that runtime has been copied across.
    """
    new = project_root / CURRENT_DIR_NAME
    old = project_root / LEGACY_DIR_NAME
    if _receipt_applied(new):
        return new
    try:
        legacy = has_runtime(old) and (not has_runtime(new) or _is_disposable_shadow(new))
    except OSError:
        legacy = has_runtime(old) and not has_runtime(new)
    if legacy:
        note_once(
            f"dir:{old}",
            f"仍在使用旧数据目录 {old}。请运行 `ampuly migrate-home --apply` 复制到 {new}（旧目录会保留）。本次回退读取旧位置。",
        )
        return old
    if new.exists() or not old.is_dir() or not any(old.iterdir()):
        return new
    note_once(
        f"dir:{old}",
        f"仍在使用旧数据目录 {old}。请运行 `ampuly migrate-home --apply` 复制到 {new}（旧目录会保留）。本次回退读取旧位置。",
    )
    return old


@dataclass
class HomeMigrationReport:
    source: Path
    dest: Path
    apply: bool
    copied: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    column_renames: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rejected: bool = False
    write_errors: list[str] = field(default_factory=list)
    preview_incomplete: bool = False
    preview_errors: list[str] = field(default_factory=list)
    moved_aside: list[str] = field(default_factory=list)
    rewritten_keys: list[str] = field(default_factory=list)
    backup: str = ""
    receipt: str = ""
    rollback_file: str = ""
    rolled_back: bool = False
    longest_resulting: str = ""

    def ok(self) -> bool:
        """True only for a complete, non-rejected dry-run/apply with no recorded failures."""
        return (
            (not self.rejected)
            and (not self.write_errors)
            and (not self.preview_incomplete)
        )

    def lines(self) -> list[str]:
        mode = "执行复制" if self.apply else "预览（未写入；加上 --apply 才会复制）"
        out = [f"迁移数据目录：{mode}", f"  从 {self.source}", f"  到 {self.dest}"]
        out.extend(self.notes)
        if self.preview_errors:
            out.append(f"预览未完成 {len(self.preview_errors)} 项：")
            out.extend(f"  ? {item}" for item in self.preview_errors[:20])
            if len(self.preview_errors) > 20:
                out.append(f"  … 另有 {len(self.preview_errors) - 20} 个")
        if self.write_errors:
            out.append(f"写入失败 {len(self.write_errors)} 项：")
            out.extend(f"  ! {item}" for item in self.write_errors[:20])
            if len(self.write_errors) > 20:
                out.append(f"  … 另有 {len(self.write_errors) - 20} 个")
        if self.copied:
            verb = "将复制" if not self.apply else "已复制"
            out.append(f"文件 {len(self.copied)} 个（{verb}）：")
            out.extend(f"  + {item}" for item in self.copied[:40])
            if len(self.copied) > 40:
                out.append(f"  … 另有 {len(self.copied) - 40} 个")
        elif not self.notes and not self.write_errors and not self.preview_errors:
            out.append("没有需要复制的文件。")
        if self.skipped_existing:
            out.append(f"目标里已有、因此保留不动的文件 {len(self.skipped_existing)} 个。")
        if self.column_renames:
            label = "将改列" if not self.apply else "已改列"
            out.append(f"数据库列（{label}）：" + "，".join(self.column_renames))
        if self.rewritten_keys:
            out.append("配置键：" + "，".join(self.rewritten_keys))
        if self.backup:
            out.append(f"一致性备份：{self.backup}")
        if self.moved_aside:
            out.append("已挪走（没有删除）：")
            out.extend(f"  → {item}" for item in self.moved_aside)
        if self.receipt:
            out.append(f"迁移回执：{self.receipt}")
        if self.rollback_file:
            out.append(f"退回说明：{self.rollback_file}")
        if self.longest_resulting:
            out.append(f"最长结果路径（{len(self.longest_resulting)} 个字符）：{self.longest_resulting}")
        out.append("旧目录没有删除。")
        if self.rejected and self.rolled_back:
            out.append("已退回。来源目录没有改动。")
        elif self.rejected:
            out.append("拒绝：未改来源，也未改目标。")
            # Repeat the reason on its own short line so a truncated assertion
            # still shows the file that stopped the migration.
            for note in self.notes:
                head = note.splitlines()[0].strip()
                if not head:
                    continue
                if len(head) <= 160:
                    out.append(head)
                else:
                    tail = head.replace("/", "\\").rsplit("\\", 1)[-1]
                    out.append(f"失败文件：{tail}")
                break
        elif self.preview_incomplete:
            out.append("预览未完成：未核验全部库结构；不能声称可安全迁移。")
        elif self.apply and self.write_errors:
            out.append("执行未完成：存在写入失败，请查看上方错误。")
        elif self.apply:
            out.append("已核对：文件哈希与各表行数在列改名前后一致。")
        elif not self.apply:
            out.append("本次为预览；没有任何文件或数据库被改写。本预览不是安全迁库批准。")
        return out


def _merge_tree(source: Path, dest: Path, apply: bool, report: HomeMigrationReport, prefix: str = "") -> None:
    if _is_link_or_junction(source) or not _exists(source):
        report.notes.append(f"跳过符号链接/Junction或不存在的路径：{source}")
        return
    if _is_file_entry(source):
        rel = prefix or source.name
        if _exists(dest):
            report.skipped_existing.append(rel)
            return
        report.copied.append(rel)
        if apply:
            try:
                _copy_file(source, dest)
            except OSError as exc:
                report.write_errors.append(f"{rel}: {exc}")
        return
    for current, dirnames, filenames in _walk(source):
        dirnames[:] = [name for name in dirnames if not _is_link_or_junction(current / name)]
        for name in filenames:
            src = current / name
            if _is_link_or_junction(src):
                report.notes.append(f"跳过符号链接/Junction：{src}")
                continue
            if _is_sqlite_sidecar(name):
                continue
            rel = src.relative_to(source).as_posix()
            if prefix:
                rel = f"{prefix}/{rel}"
            target = dest / src.relative_to(source)
            if _exists(target):
                report.skipped_existing.append(rel)
                continue
            report.copied.append(rel)
            if apply:
                try:
                    _copy_file(src, target)
                except OSError as exc:
                    report.write_errors.append(f"{rel}: {exc}")


def _alias_directories(root: Path, old_name: str, new_name: str, apply: bool, report: HomeMigrationReport) -> None:
    if not _is_dir_entry(root) or _is_link_or_junction(root):
        return
    for current, dirnames, _filenames in _walk(root):
        if old_name not in dirnames:
            continue
        src = current / old_name
        dest = current / new_name
        dirnames.remove(old_name)
        _merge_tree(src, dest, apply, report, prefix=src.relative_to(root).as_posix())


def rename_legacy_columns_sync(conn: sqlite3.Connection) -> list[str]:
    """Rename leftover identity columns. Missing tables are ignored. Data stays."""
    changed: list[str] = []
    for table, old, new in _LEGACY_COLUMNS:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {row[1] for row in rows}
        if old in names and new not in names:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
            changed.append(f"{table}.{old}->{new}")
    if changed:
        conn.commit()
    return changed


def _rename_databases(root: Path, apply: bool, report: HomeMigrationReport) -> None:
    if not _is_dir_entry(root) or _is_link_or_junction(root):
        return
    for current, dirnames, filenames in _walk(root):
        dirnames[:] = [name for name in dirnames if not _is_link_or_junction(current / name)]
        for name in filenames:
            if not name.endswith((".db", ".sqlite", ".sqlite3")):
                continue
            path = current / name
            if _is_link_or_junction(path) or not _is_file_entry(path):
                continue
            try:
                conn = _ro_connect(path)
            except sqlite3.Error as exc:
                report.preview_incomplete = True
                report.preview_errors.append(f"预览未完成：无法只读打开 {path}：{exc}")
                continue
            try:
                pending = []
                for table, old, new in _LEGACY_COLUMNS:
                    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
                    names = {row[1] for row in rows}
                    if old in names and new not in names:
                        pending.append(f"{path.name}:{table}.{old}->{new}")
            except sqlite3.Error as exc:
                report.preview_incomplete = True
                report.preview_errors.append(f"预览未完成：无法读取 {path} 表结构：{exc}")
                pending = []
            finally:
                conn.close()
            if not pending:
                continue
            report.column_renames.extend(pending)
            if not apply:
                continue
            try:
                writable = _connect_rw(path, timeout=5.0)
            except sqlite3.Error as exc:
                report.write_errors.append(f"未能打开数据库 {path}：{exc}")
                continue
            try:
                rename_legacy_columns_sync(writable)
            except sqlite3.Error as exc:
                report.write_errors.append(f"未能改列 {path}：{exc}")
            finally:
                writable.close()


async def rename_legacy_columns(db) -> list[str]:
    """Async wrapper for the office UI and the runtime store connection."""
    changed: list[str] = []
    for table, old, new in _LEGACY_COLUMNS:
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
        names = {row[1] for row in rows}
        if old in names and new not in names:
            await db.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
            changed.append(f"{table}.{old}->{new}")
    if changed:
        await db.commit()
    return changed


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def _sibling(path: Path, suffix: str) -> Path:
    candidate = path.parent / f"{path.name}.{suffix}"
    serial = 2
    while _exists(candidate):
        candidate = path.parent / f"{path.name}.{suffix}-{serial}"
        serial += 1
    return candidate


def _refuse(report: HomeMigrationReport, message: str) -> HomeMigrationReport:
    report.rejected = True
    report.notes.append(message)
    return report


def _walk_files(root: Path) -> list[Path]:
    found: list[Path] = []
    if not _is_dir_entry(root) or _is_link_or_junction(root):
        return found
    for current, dirnames, filenames in _walk(root):
        dirnames[:] = [name for name in dirnames if not _is_link_or_junction(current / name)]
        for name in filenames:
            path = current / name
            if _is_link_or_junction(path) or not _is_file_entry(path):
                continue
            found.append(path)
    return found


def _hash_tree(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): _sha256(path) for path in _walk_files(root)}


def _drift(root: Path, files: dict[str, str]) -> str | None:
    current = _hash_tree(root)
    for rel, digest in files.items():
        if current.get(rel) != digest:
            return rel
    extras = sorted(set(current) - set(files))
    if extras:
        return extras[0]
    return None


class _TreeGuard:
    """Restore SQLite sidecars created by a read, and notice if real bytes changed."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: dict[str, str] = {}
        self.small: dict[str, bytes] = {}
        self.problem: str | None = None

    def __enter__(self) -> "_TreeGuard":
        if _is_dir_entry(self.root) and not _is_link_or_junction(self.root):
            self.files = _hash_tree(self.root)
            for rel, _digest in self.files.items():
                name = Path(rel).name
                if name.endswith("-shm") or name.endswith("-journal"):
                    self.small[rel] = _read_bytes(self.root / rel)
        return self

    def repair(self) -> None:
        """Put sidecar bytes back after a read. Real database and WAL bytes must still match."""
        if not _is_dir_entry(self.root) or _is_link_or_junction(self.root):
            return
        self.problem = None
        for rel, data in self.small.items():
            path = self.root / rel
            try:
                if not _exists(path) or _read_bytes(path) != data:
                    _write_bytes(path, data)
            except OSError as err:
                self.problem = f"{rel}: {err}"
        mains_match = self.problem is None
        if mains_match:
            for rel, digest in self.files.items():
                if _is_sqlite_sidecar(Path(rel).name):
                    continue
                path = self.root / rel
                if not _is_file_entry(path) or _sha256(path) != digest:
                    self.problem = rel
                    mains_match = False
                    break
        if mains_match:
            for path in _walk_files(self.root):
                rel = path.relative_to(self.root).as_posix()
                if rel in self.files or not _is_sqlite_sidecar(path.name):
                    continue
                try:
                    _unlink(path)
                except OSError as err:
                    self.problem = f"{rel}: {err}"
        if self.problem is None:
            self.problem = _drift(self.root, self.files)

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.repair()
        return False


@dataclass(frozen=True)
class _ReparseFound:
    """A link, or an entry whose attributes could not be read. Both refuse apply."""

    path: str
    unreadable: bool = False


def _direntry_reparse(entry: os.DirEntry) -> bool | None:
    """Whether one directory entry is a symlink, junction, or other reparse point.

    ``None`` means the attributes could not be read. Callers refuse that entry.
    On Windows ``is_symlink``, ``is_junction``, and ``stat(follow_symlinks=False)``
    come from FindFirstFile/FindNextFile and do not open the file, so a mapped
    SQLite sidecar is classified without a sharing-violation stat. A regular
    mode does not clear ``FILE_ATTRIBUTE_REPARSE_POINT``.
    """
    try:
        if entry.is_symlink():
            return True
        is_junction = getattr(entry, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    if stat.S_ISLNK(getattr(info, "st_mode", 0)):
        return True
    if bool(getattr(info, "st_file_attributes", 0) & _REPARSE):
        return True
    return False


def _scan_directory(directory: Path):
    """List ``directory`` without following links. Windows uses the extended prefix."""
    location = extended_io_path(directory) if os.name == "nt" else os.fspath(directory)
    with os.scandir(location) as children:
        yield from children


def _find_reparse(root: Path) -> _ReparseFound | None:
    """First symlink, junction, reparse point, or unreadable entry under ``root``.

    Children are classified from directory-entry attributes, not from a second
    ``lstat`` and not from the file name. There is no lock exemption: if the
    attributes cannot be read, the entry is reported.
    """
    if _is_link_or_junction(root):
        return _ReparseFound(str(root))
    ancestor = _ancestor_reparse(root)
    if ancestor is not None:
        return _ReparseFound(str(ancestor))
    if not _is_dir_entry(root):
        return None
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = list(_scan_directory(current))
        except OSError:
            return _ReparseFound(str(current), unreadable=True)
        children: list[Path] = []
        for entry in entries:
            logical = current / entry.name
            verdict = _direntry_reparse(entry)
            if verdict is None:
                return _ReparseFound(str(logical), unreadable=True)
            if verdict:
                return _ReparseFound(str(logical))
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                return _ReparseFound(str(logical), unreadable=True)
            if is_dir:
                children.append(logical)
        pending.extend(reversed(children))
    return None


def _reparse_message(found: _ReparseFound) -> str:
    if found.unreadable:
        return f"拒绝：读不到目录项属性，不能确认它不是符号链接或 Junction：{found.path}"
    return f"拒绝：遇到符号链接或 Junction，不会跟着走出去：{found.path}"


def _table_digest(conn: sqlite3.Connection, table: str) -> str:
    quoted = _quote_ident(table)
    info = list(conn.execute(f"PRAGMA table_info({quoted})"))
    physical = [str(row[1]) for row in info]
    pairs = sorted((_CANONICAL_COLUMN.get(name, name), name) for name in physical)
    header = "|".join(name for name, _physical in pairs)
    select = ", ".join(_quote_ident(physical) for _name, physical in pairs) or "rowid"
    digest = hashlib.sha256()
    digest.update((header + "\n").encode())
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    without_rowid = bool(ddl and ddl[0] and "WITHOUT ROWID" in str(ddl[0]).upper())
    order = select if without_rowid else "rowid"
    try:
        cursor = conn.execute(f"SELECT {select} FROM {quoted} ORDER BY {order}")
    except sqlite3.Error:
        cursor = conn.execute(f"SELECT {select} FROM {quoted}")
    for row in cursor:
        digest.update(repr(tuple(row)).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _sqlite_identity(path: Path) -> tuple[dict[str, int], dict[str, str]] | None:
    try:
        conn = _ro_connect(path)
    except sqlite3.Error:
        return None
    try:
        try:
            tables = [str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )]
        except sqlite3.Error:
            return None
        counts: dict[str, int] = {}
        digests: dict[str, str] = {}
        for table in tables:
            try:
                counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}").fetchone()[0])
                digests[table] = _table_digest(conn, table)
            except sqlite3.Error:
                return None
        return counts, digests
    finally:
        conn.close()


def _rewrite_env_text(text: str) -> tuple[str, list[str]]:
    """Rename ``OPC_*`` keys to ``AMPULY_*`` when the new key is not already set. Values stay."""
    lines = text.splitlines(keepends=True)
    defined: set[str] = set()
    parsed: list[tuple[str, re.Match[str] | None]] = []
    for line in lines:
        body = line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith("\n") else line
        match = _ENV_LINE.match(body)
        if match:
            defined.add(match.group("key"))
        parsed.append((line, match))
    changed: list[str] = []
    out: list[str] = []
    for line, match in parsed:
        if match is None or not match.group("key").startswith("OPC_") or len(match.group("key")) <= 4:
            out.append(line)
            continue
        canonical = "AMPULY_" + match.group("key")[4:]
        if canonical in defined:
            out.append(line)
            continue
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        export = match.group("export") or ""
        out.append(f"{export}{canonical}={match.group('value')}{ending}")
        changed.append(f"{match.group('key')}->{canonical}")
        defined.add(canonical)
    return "".join(out), changed


def _file_token(path: Path, *, rewrite_env: bool) -> str:
    raw = _read_bytes(path)
    if rewrite_env and _is_env_file(path):
        try:
            text, _keys = _rewrite_env_text(raw.decode("utf-8"))
        except UnicodeError:
            pass
        else:
            raw = text.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _describe(root: Path, *, rewrite_env: bool) -> tuple[dict[str, str], dict[str, int], dict[str, dict[str, str]]]:
    files: dict[str, str] = {}
    tables: dict[str, int] = {}
    digests: dict[str, dict[str, str]] = {}
    for path in _walk_files(root):
        rel = path.relative_to(root).as_posix()
        if _is_sqlite_sidecar(path.name):
            continue
        if _is_sqlite_db(path.name):
            identity = _sqlite_identity(path)
            if identity is None:
                files[rel] = ""
                continue
            counts, digest = identity
            files[rel] = "sqlite"
            digests[rel] = digest
            for table, count in counts.items():
                tables[f"{rel}/{table}"] = count
            continue
        files[rel] = _file_token(path, rewrite_env=rewrite_env)
    return files, tables, digests


def _alias_matches(rel: str, source: Path, dest: Path) -> bool:
    parts = rel.split("/")
    for index, part in enumerate(parts):
        for old, new in _DIR_ALIASES:
            if part != new:
                continue
            swapped = parts[:]
            swapped[index] = old
            origin = source / "/".join(swapped)
            copied = dest / rel
            if not _is_file_entry(origin) or _is_link_or_junction(origin) or _is_link_or_junction(copied):
                continue
            if _is_sqlite_db(copied.name):
                left = _sqlite_identity(origin)
                right = _sqlite_identity(copied)
                if left is not None and left == right:
                    return True
                continue
            if _file_token(origin, rewrite_env=True) == _file_token(copied, rewrite_env=False):
                return True
    return False


def _compare_trees(source: Path, dest: Path, *, rewrite_env: bool, allow_aliases: bool) -> str | None:
    src_files, src_tables, src_digests = _describe(source, rewrite_env=rewrite_env)
    dst_files, dst_tables, dst_digests = _describe(dest, rewrite_env=False)
    for rel, token in src_files.items():
        if rel not in dst_files:
            return f"目标缺少文件 {rel}"
        if token == "sqlite" or dst_files[rel] == "sqlite":
            if token != "sqlite" or dst_files[rel] != "sqlite":
                return f"数据库形态不一致：{rel}"
            if src_digests.get(rel) != dst_digests.get(rel):
                return f"行数或内容不一致：{rel}"
            continue
        if token != dst_files[rel]:
            return f"文件哈希不一致：{rel}"
    for key, count in src_tables.items():
        if dst_tables.get(key) != count:
            return f"行数不一致：{key} {count}!={dst_tables.get(key)}"
    for rel in dst_files:
        if rel in src_files:
            continue
        if allow_aliases and _alias_matches(rel, source, dest):
            continue
        return f"目标多出了无法对应的文件 {rel}"
    return None


def _identity_problem(source: Path, dest: Path) -> str | None:
    """Final check: destination matches the source, after planned renames."""
    return _compare_trees(source, dest, rewrite_env=True, allow_aliases=True)


def _backup_matches(source: Path, backup: Path) -> str | None:
    return _compare_trees(source, backup, rewrite_env=False, allow_aliases=False)


def _shadow_blockers(dest: Path) -> list[str]:
    blockers: list[str] = []
    saw_db = False
    if not _is_dir_entry(dest) or _is_link_or_junction(dest):
        return ["目标不是普通目录"]
    for current, dirnames, filenames in _walk(dest):
        for name in list(dirnames):
            path = current / name
            if _is_link_or_junction(path):
                blockers.append(f"符号链接或 Junction：{path}")
                dirnames.remove(name)
        for name in filenames:
            path = current / name
            rel = path.relative_to(dest).as_posix()
            if _is_link_or_junction(path):
                blockers.append(f"符号链接或 Junction：{path}")
                continue
            top = rel.split("/", 1)[0]
            if _is_sqlite_sidecar(name):
                if _lstat(path).st_size > 0:
                    blockers.append(f"{rel} 还有未合并的 WAL/SHM")
                continue
            if _is_sqlite_db(name):
                saw_db = True
                identity = _sqlite_identity(path)
                if identity is None:
                    blockers.append(f"{rel} 打不开")
                elif sum(identity[0].values()):
                    blockers.append(f"{rel} 有 {sum(identity[0].values())} 行")
                continue
            if top == "accounts.json" and _lstat(path).st_size > 2:
                blockers.append("accounts.json 里有内容")
                continue
            if top in _USER_MATERIAL:
                blockers.append(f"{rel} 是用户资料，不是空库影子")
                continue
            if top == "projects":
                blockers.append(f"{rel} 不是项目库文件")
                continue
            if top not in _SHADOW_TOP:
                blockers.append(f"{rel} 不是空库影子里的生成文件")
    if not saw_db:
        blockers.append("没有数据库，不能证明这是空库影子")
    return blockers


def _is_disposable_shadow(path: Path) -> bool:
    try:
        signature = tuple(
            (item.relative_to(path).as_posix(), _lstat(item).st_mtime_ns, _lstat(item).st_size)
            for item in _walk_files(path)
        )
    except OSError:
        return False
    cached = _SHADOW_CACHE.get(str(path))
    if cached is not None and cached[0] == signature:
        return cached[1]
    ok = False
    try:
        with _TreeGuard(path) as guard:
            ok = not _shadow_blockers(path)
        if guard.problem:
            ok = False
    except OSError:
        ok = False
    _SHADOW_CACHE[str(path)] = (signature, ok)
    return ok


def _receipt_applied(home: Path) -> bool:
    path = home / RECEIPT_NAME
    if not _is_file_entry(path) or _is_link_or_junction(path):
        return False
    try:
        data = json.loads(_read_text(path))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("status") == "applied" and int(data.get("version") or 0) == RECEIPT_VERSION


def _console_ports(console_ports: tuple[int, ...] | None) -> tuple[int, ...]:
    if console_ports is not None:
        return console_ports
    from ampuly.cli.maintenance import CONSOLE_PORTS

    return CONSOLE_PORTS


def _in_use_reason(homes: list[Path | None], ports: tuple[int, ...]) -> str | None:
    from ampuly.cli.maintenance import console_holds_home, console_listening

    seen: list[Path] = []
    for home in homes:
        if home is None or home in seen or not _exists(home):
            continue
        seen.append(home)
        try:
            held = console_holds_home(home)
        except OSError:
            return f"无法检查 {home} 的 office_ui.lock，为避免写到正在使用的目录，已拒绝。"
        if held:
            return f"控制台正在使用 {home}（office_ui.lock 被占用）。先停掉控制台再迁移。"
    for port in ports:
        if console_listening(int(port)):
            return f"端口 {port} 上有控制台在应答。先停掉 ampuly ui 再迁移。"
    return None


def _busy_database(root: Path) -> str | None:
    for path in _walk_files(root):
        if not _is_sqlite_db(path.name):
            continue
        try:
            conn = _connect_rw(path, timeout=0)
        except sqlite3.Error as exc:
            return f"{path}（{exc}）"
        try:
            # A write-lock probe must not checkpoint a WAL into the main file.
            try:
                conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
            except (AttributeError, sqlite3.Error):
                pass
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        except sqlite3.OperationalError:
            return str(path)
        finally:
            conn.close()
    return None


def _backup_sqlite(source: Path, target: Path) -> None:
    src = _ro_connect(source)
    try:
        dst = _connect_rw(target, timeout=5.0)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    for suffix in ("-wal", "-shm", "-journal"):
        side = Path(str(target) + suffix)
        if _exists(side) and not _is_link_or_junction(side) and _is_file_entry(side):
            _unlink(side)


def _transfer_tree(source: Path, dest: Path, *, sqlite_backup: bool, report: HomeMigrationReport | None) -> None:
    _mkdir(dest)
    for current, dirnames, filenames in _walk(source):
        relative = current.relative_to(source)
        _mkdir(dest / relative)
        kept: list[str] = []
        for name in dirnames:
            path = current / name
            if _is_link_or_junction(path):
                raise _ApplyFailure(f"拒绝：符号链接或 Junction，不会跟着走出去：{path}")
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            src = current / name
            if _is_link_or_junction(src):
                raise _ApplyFailure(f"拒绝：符号链接或 Junction，不会跟着走出去：{src}")
            # A real sidecar is folded by the SQLite backup API. The link check
            # above already ran: a junction named like a sidecar is not skipped.
            if _is_sqlite_sidecar(name):
                continue
            if not _is_file_entry(src):
                raise _ApplyFailure(f"拒绝：非常规文件 {src}")
            rel = src.relative_to(source).as_posix()
            target = dest / relative / name
            if sqlite_backup and _is_sqlite_db(name):
                _backup_sqlite(src, target)
            else:
                _copy_file(src, target)
            if report is not None and not sqlite_backup:
                report.copied.append(rel)


def _rewrite_env_tree(root: Path) -> list[str]:
    changed: list[str] = []
    for path in _walk_files(root):
        if not _is_env_file(path):
            continue
        raw = _read_bytes(path)
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            continue
        rewritten, keys = _rewrite_env_text(text)
        blob = rewritten.encode("utf-8")
        if keys and blob != raw:
            _write_bytes(path, blob)
            prefix = path.relative_to(root).as_posix()
            changed.extend(f"{prefix}:{item}" for item in keys)
    return changed


def _clear_sidecars(root: Path) -> None:
    for path in list(_walk_files(root)):
        if _is_sqlite_sidecar(path.name) and not _is_link_or_junction(path):
            _unlink(path)


def _discard_backup(path: Path, source: Path) -> None:
    if path.parent != source.parent or not path.name.startswith(source.name + ".pre-migrate-"):
        raise _ApplyFailure(f"备份路径异常，没有删除：{path}")
    _remove_tree(path)


def _instructions(*, source: Path, dest: Path, backup: str, aside: str, reason: str) -> str:
    lines = [reason, "来源目录没有删除，也没有被修改。", f"来源：{source}"]
    if backup:
        lines.append(f"一致性备份：{backup}")
    lines.append(f"目标：{dest}")
    if aside:
        lines.append(f"挪走的目录：{aside}")
    lines.append("要退回：先停掉控制台，再运行 ampuly migrate-home --rollback。")
    lines.append("不要删除来源。退回会把这次的目标改名留档，并把当初挪走的旧目标放回（如果当时有）。")
    return "\n".join(lines) + "\n"


@dataclass
class _Txn:
    backup: Path | None = None
    aside: Path | None = None
    started_dest: bool = False
    trust_carried: bool = False
    trust_before: dict[str, str] | None = None


def _rollback_dest(dest: Path, txn: _Txn, report: HomeMigrationReport, reason: str, now: datetime | None) -> None:
    partial: Path | None = None
    try:
        if txn.started_dest and _exists(dest):
            partial = _sibling(dest, f"partial-{_stamp(now)}")
            _rename(dest, partial)
        if txn.aside and _exists(txn.aside) and not _exists(dest):
            _rename(txn.aside, dest)
    except OSError as exc:
        reason = f"{reason}；退回时还遇到：{exc}"
    report.rolled_back = True
    report.rejected = True
    report.write_errors.append(reason)
    text = _instructions(
        source=report.source,
        dest=dest,
        backup=str(txn.backup or ""),
        aside=str(partial or txn.aside or ""),
        reason="已退回。" + reason,
    )
    report.notes.append(text)
    for folder in (partial, txn.backup):
        if folder is not None and _is_dir_entry(folder) and not _is_link_or_junction(folder):
            try:
                _write_text(folder / ROLLBACK_NAME, text)
                report.rollback_file = str(folder / ROLLBACK_NAME)
            except OSError:
                pass
    _SHADOW_CACHE.pop(str(dest), None)


def _nonempty(path: Path) -> bool:
    if not _is_dir_entry(path) or _is_link_or_junction(path):
        return False
    try:
        with os.scandir(extended_io_path(path)) as children:
            return next(children, None) is not None
    except OSError:
        return False


def migration_config_dirs(project_root: Path) -> tuple[str, str] | None:
    """Old and new config directories recorded by a completed migration, if any."""

    path = Path(project_root) / CURRENT_DIR_NAME / RECEIPT_NAME
    try:
        data = json.loads(_read_text(path))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("status") != "applied":
        return None
    source = str(data.get("source") or "").strip()
    dest = str(data.get("dest") or "").strip()
    if not source or not dest:
        return None
    return str(Path(source) / "config"), str(Path(dest) / "config")


def _authority_file_rows(source_config: Path, dest_config: Path) -> list[dict[str, Any]]:
    from ampuly.core.workspace_trust import _AUTHORITY_SOURCE_FILES, authority_file_token

    rows: list[dict[str, Any]] = []
    for name in _AUTHORITY_SOURCE_FILES:
        source_token = authority_file_token(source_config / name)
        dest_token = authority_file_token(dest_config / name)
        rows.append({
            "name": name,
            "source": source_token,
            "dest": dest_token,
            "same": source_token == dest_token,
        })
    return rows


def _load_config_without_mutating(config_dir: Path) -> tuple[Any, bool]:
    """Parse a copy of ``config_dir``. The bool is true when that parse rewrites authority files."""

    import tempfile

    from ampuly.core.config import AmpulyConfig
    from ampuly.core.workspace_trust import _AUTHORITY_SOURCE_FILES, authority_file_token

    parent = Path(tempfile.mkdtemp(prefix="ampuly-trust-"))
    try:
        copied = parent / "config"
        shutil.copytree(config_dir, copied, symlinks=True)
        before = {name: authority_file_token(copied / name) for name in _AUTHORITY_SOURCE_FILES}
        loaded = AmpulyConfig.load(copied, trusted_source=True)
        after = {name: authority_file_token(copied / name) for name in _AUTHORITY_SOURCE_FILES}
        return loaded, before != after
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def _trust_fingerprints(record: dict[str, Any] | None) -> dict[str, str] | None:
    """Two fingerprints copied from a trust-store entry. No workspace path."""

    if not isinstance(record, dict):
        return None
    source_fingerprint = record.get("source_fingerprint")
    authority_fingerprint = record.get("authority_fingerprint")
    if not (
        isinstance(source_fingerprint, str)
        and source_fingerprint.startswith("sha256:")
        and isinstance(authority_fingerprint, str)
        and authority_fingerprint.startswith("sha256:")
    ):
        return None
    return {
        "source_fingerprint": source_fingerprint,
        "authority_fingerprint": authority_fingerprint,
    }


def _migration_trust_backup_path(workspace: Path) -> Path:
    """Backup of one workspace entry, beside the trust store, not inside the project."""

    from ampuly.core.workspace_trust import WorkspaceTrustStore, canonical_workspace

    key = hashlib.sha256(str(canonical_workspace(workspace)).encode("utf-8")).hexdigest()
    return WorkspaceTrustStore().path.parent / "migration-trust" / f"{key}.json"


def _sealed_trust_backup(workspace: Path, previous: dict[str, str] | None, created_source: str, created_authority: str) -> dict[str, Any]:
    from ampuly.core.workspace_trust import canonical_workspace

    payload = {
        "version": 1,
        "workspace": str(canonical_workspace(workspace)),
        "previous": previous,
        "created_source_fingerprint": created_source,
        "created_authority_fingerprint": created_authority,
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "payload": payload,
        "integrity": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def _write_trust_backup(workspace: Path, previous: dict[str, str] | None, created_source: str, created_authority: str) -> None:
    """Write the prior entry, fsync it, and read it back. Raise if it does not verify."""

    path = _migration_trust_backup_path(workspace)
    sealed = _sealed_trust_backup(workspace, previous, created_source, created_authority)
    raw = (json.dumps(sealed, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _mkdir(path.parent)
    temporary = path.parent / f".{path.name}.tmp"
    try:
        with open(io_path(temporary), "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(io_path(temporary), io_path(path))
    except Exception:
        if _exists(temporary) and not _is_link_or_junction(temporary):
            try:
                _unlink(temporary)
            except OSError:
                pass
        raise
    verified = _read_trust_backup(workspace)
    verified_previous = _previous_from_backup(verified) if verified is not None else False
    if (
        verified is None
        or verified.get("created_source_fingerprint") != created_source
        or verified.get("created_authority_fingerprint") != created_authority
        or verified_previous is False
        or verified_previous != previous
    ):
        _delete_trust_backup(workspace)
        raise OSError("trust backup failed integrity check")


def _read_trust_backup(workspace: Path) -> dict[str, Any] | None:
    """Return the payload only when its integrity hash and workspace match."""

    from ampuly.core.workspace_trust import canonical_workspace

    try:
        raw = json.loads(_read_text(_migration_trust_backup_path(workspace)))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    payload = raw.get("payload")
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != 1:
        return None
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    if raw.get("integrity") != digest:
        return None
    if payload.get("workspace") != str(canonical_workspace(workspace)):
        return None
    return payload


def _delete_trust_backup(workspace: Path) -> None:
    path = _migration_trust_backup_path(workspace)
    if _exists(path) and not _is_link_or_junction(path):
        try:
            _unlink(path)
        except OSError:
            pass


def _previous_from_backup(payload: dict[str, Any]) -> dict[str, str] | None | bool:
    """``None`` means there was no prior entry. ``False`` means the backup is unusable."""

    previous = payload.get("previous")
    if previous is None:
        return None
    parsed = _trust_fingerprints(previous)
    if parsed is None:
        return False
    return parsed


def describe_config_trust(project_root: Path, source: Path, dest: Path) -> str:
    """What apply will do with trust. Reads the stores and does not write them."""

    from ampuly.core.workspace_trust import (
        authority_grant_matches,
        canonical_workspace,
        matching_config_trust,
    )

    source_config = source / "config"
    dest_config = dest / "config"
    if not _is_dir_entry(source_config) or _is_link_or_junction(source_config):
        return f"信任记录：{source_config} 不是可核对的配置目录，没有信任可带。"
    workspace = canonical_workspace(project_root)
    match = matching_config_trust(workspace, source_config)
    if match is None:
        return (
            f"信任记录：{source_config} 没有匹配的信任记录，不会带到 {dest_config}。"
            "未信任的来源保持未信任。"
        )
    try:
        loaded, mutated = _load_config_without_mutating(source_config)
    except Exception as exc:
        return f"信任记录：来源配置读不出来，不会带到 {dest_config}：{exc}"
    if mutated or not authority_grant_matches(str(match["kind"]), match["record"], loaded):
        return f"信任记录：{source_config} 的生效权限和当初信任的不一致，不会带到 {dest_config}。"
    rows = _authority_file_rows(source_config, source_config)
    listed = "，".join(f"{row['name']}（{row['source']}）" for row in rows)
    return (
        f"信任记录：将把 {workspace} 从 {source_config} 改记到 {dest_config}，"
        f"记录在 {match['store']}（{match['kind']}）。"
        f"只带复制后仍逐字节一致的文件：{listed}。"
        "内容有变化或新出现的文件不会写入信任记录。"
        "退回只撤掉这次写下的记录；更早的记录只从信任库目录里的备份恢复。"
    )


def carry_config_trust(project_root: Path, source: Path, dest: Path) -> dict[str, Any]:
    """Re-key a trusted source config onto ``dest`` when every authority file matches.

    The gate is not changed. A file whose bytes differ, or a file that only
    exists after the copy, leaves the trust record unmoved.
    """

    from ampuly.core.workspace_trust import (
        WorkspaceTrustRequired,
        WorkspaceTrustStore,
        authority_grant_matches,
        canonical_workspace,
        matching_config_trust,
    )

    source_config = source / "config"
    dest_config = dest / "config"
    workspace = canonical_workspace(project_root)
    store = WorkspaceTrustStore()
    before = _trust_fingerprints(store._record(workspace))
    rows = _authority_file_rows(source_config, dest_config) if _is_dir_entry(source_config) else []
    result: dict[str, Any] = {
        "carried": False,
        "source_config": str(source_config),
        "dest_config": str(dest_config),
        "files": rows,
        "note": "",
    }
    withheld = [row["name"] for row in rows if not row["same"]]
    match = None
    if _is_dir_entry(source_config) and not _is_link_or_junction(source_config):
        match = matching_config_trust(workspace, source_config)
    if match is None:
        result["note"] = f"信任记录：{source_config} 没有匹配的信任记录，不会带到 {dest_config}。"
        return result
    if not _is_dir_entry(dest_config) or _is_link_or_junction(dest_config):
        result["note"] = f"信任记录：{dest_config} 不是可核对的配置目录，信任不带走。"
        return result
    if withheld:
        result["note"] = "信任记录：下列文件和来源不一致，信任不带走：" + "，".join(withheld)
        return result
    try:
        loaded, mutated = _load_config_without_mutating(dest_config)
    except Exception as exc:
        result["note"] = f"信任记录：新目录的配置读不出来，信任不带走：{exc}"
        return result
    if mutated or not authority_grant_matches(str(match["kind"]), match["record"], loaded):
        result["note"] = "信任记录：生效权限和当初信任的不一致，或读取时会改写文件，信任不带走。"
        return result
    from ampuly.core.workspace_trust import authority_source_fingerprint, effective_authority_fingerprint

    created = {
        "source_fingerprint": authority_source_fingerprint(dest_config),
        "authority_fingerprint": effective_authority_fingerprint(loaded),
    }
    # The backup of the prior entry is durable before the formal store changes.
    _write_trust_backup(workspace, before, created["source_fingerprint"], created["authority_fingerprint"])
    try:
        # One key is replaced. Every other workspace entry stays. The write is a
        # temp file, fsync, and os.replace inside the trust store.
        store.restore_workspace_entry(workspace, created)
        store.require(workspace, dest_config, loaded)
    except Exception:
        _delete_trust_backup(workspace)
        current = _trust_fingerprints(store._record(workspace))
        if current != before:
            store.restore_workspace_entry(workspace, before)
        raise
    written = _trust_fingerprints(store._record(workspace))
    if written != created:
        store.restore_workspace_entry(workspace, before)
        _delete_trust_backup(workspace)
        result["note"] = "信任记录：改记之后和磁盘上的配置对不上，已恢复原记录。"
        return result
    result["carried"] = True
    result["before"] = before
    names = "，".join(row["name"] for row in rows)
    result["note"] = (
        f"信任记录：已把 {workspace} 从 {source_config} 改记到 {dest_config}"
        f"（{names} 逐字节一致）。回执不保存指纹。"
    )
    return result


def _receipt_claims_other_workspace(project_root: Path, receipt: dict[str, Any]) -> bool:
    """True when the receipt names a workspace that is not this project.

    A missing claim is not a conflict. The claim is never used as a path to write.
    """

    from ampuly.core.workspace_trust import canonical_workspace

    info = receipt.get("config_trust")
    if not isinstance(info, dict):
        return False
    claimed = info.get("workspace")
    if not isinstance(claimed, str) or not claimed.strip():
        return False
    try:
        return canonical_workspace(claimed) != canonical_workspace(project_root)
    except OSError:
        return True


def undo_carried_trust(project_root: Path, config_dir: Path) -> str:
    """Remove the trust record this apply wrote for this project's config.

    The workspace is ``project_root``. Fingerprints are recomputed from
    ``config_dir`` and compared with the trust store. A prior entry is restored
    only from the integrity-checked backup beside the trust store. Nothing in
    the migration receipt is written.
    """

    from ampuly.core.workspace_trust import (
        WorkspaceTrustStore,
        authority_source_fingerprint,
        canonical_workspace,
        effective_authority_fingerprint,
        legacy_authority_fingerprint,
    )

    workspace = canonical_workspace(project_root)
    try:
        config_home = canonical_workspace(Path(config_dir).parent.parent)
    except OSError:
        return "信任记录：配置路径无法解析，没有改信任库。"
    if config_home != workspace:
        return "信任记录：配置目录不属于这个项目，没有改信任库。"
    if _is_link_or_junction(config_dir) or not _is_dir_entry(config_dir):
        return "信任记录：没有可核对的配置目录，没有改信任库。"
    store = WorkspaceTrustStore()
    record = store._record(workspace)
    if record is None:
        return "信任记录：信任库里没有这个项目的记录，没有写入新的信任。"
    recomputed = authority_source_fingerprint(config_dir)
    if record["source_fingerprint"] != recomputed:
        return "信任记录：信任库里的记录和磁盘上的配置不一致，没有改信任库。"
    try:
        loaded, mutated = _load_config_without_mutating(config_dir)
    except Exception as exc:
        return f"信任记录：配置读不出来，没有改信任库：{exc}"
    if mutated:
        return "信任记录：读取配置会改写文件，没有改信任库。"
    authority = record["authority_fingerprint"]
    if authority not in {effective_authority_fingerprint(loaded), legacy_authority_fingerprint(loaded)}:
        return "信任记录：权限指纹和磁盘上的配置不一致，没有改信任库。"
    backup = _read_trust_backup(workspace)
    if (
        backup is None
        or backup.get("created_source_fingerprint") != recomputed
        or backup.get("created_authority_fingerprint") != authority
    ):
        return "信任记录：没有核对过的备份，没有改信任库。"
    parsed = _previous_from_backup(backup)
    if parsed is False:
        return "信任记录：备份不完整，没有改信任库。"
    store.restore_workspace_entry(workspace, parsed)
    _delete_trust_backup(workspace)
    if parsed is None:
        return "信任记录：已去掉这次迁移写下的记录。旧的 openopc 信任还在。"
    return "信任记录：已按信任库里的备份恢复迁移前的记录。"


def _apply(report: HomeMigrationReport, src: Path, dst: Path, *, project_root: Path, replace_target: bool, now: datetime | None, ports: tuple[int, ...]) -> HomeMigrationReport:
    found = _find_reparse(src)
    if _exists(dst):
        found = found or _find_reparse(dst)
    else:
        ancestor = _ancestor_reparse(dst)
        found = found or (_ReparseFound(str(ancestor)) if ancestor is not None else None)
    if found:
        return _refuse(report, _reparse_message(found))
    if _receipt_applied(dst):
        report.receipt = str(dst / RECEIPT_NAME)
        report.notes.append("目标里已有迁移回执，没有再次复制。要重做请先 ampuly migrate-home --rollback。")
        return report
    busy = _in_use_reason([src, dst], ports)
    if busy:
        return _refuse(report, "拒绝：" + busy)
    kind = ""
    if _nonempty(dst):
        try:
            with _TreeGuard(dst) as guard:
                blockers = _shadow_blockers(dst)
            if guard.problem:
                blockers.append(f"检查目标时文件发生了变化：{guard.problem}")
        except OSError as exc:
            return _refuse(report, f"拒绝：无法检查目标：{exc}")
        if blockers and not replace_target:
            shown = "；".join(blockers[:8])
            return _refuse(
                report,
                f"拒绝：目标 {dst} 不是空库影子（{shown}）。"
                "要仍把整个目标挪到旁边再迁移（不删除），请加 --replace-target。",
            )
        kind = "shadow" if not blockers else "replaced"
    stamp = _stamp(now)
    backup = _sibling(src, f"pre-migrate-{stamp}")
    try:
        with _TreeGuard(src) as guard:
            locked = _busy_database(src)
            if locked:
                raise _ApplyFailure(f"数据库正在被占用：{locked}。先停掉正在使用它的进程再迁移。")
            _transfer_tree(src, backup, sqlite_backup=True, report=None)
            problem = _backup_matches(src, backup)
            if problem:
                raise _ApplyFailure(f"一致性备份和来源对不上：{problem}")
        if guard.problem:
            raise _ApplyFailure(f"读取来源时文件发生了变化：{guard.problem}")
        source_hashes = dict(guard.files)
    except _ApplyFailure as exc:
        if _exists(backup):
            _discard_backup(backup, src)
        return _refuse(report, "拒绝：" + str(exc))
    report.backup = str(backup)
    txn = _Txn(backup=backup)
    try:
        if kind:
            aside = _sibling(dst, f"{kind}-{stamp}")
            _rename(dst, aside)
            txn.aside = aside
            report.moved_aside.append(str(aside))
        _mkdir(dst)
        txn.started_dest = True
        _transfer_tree(backup, dst, sqlite_backup=False, report=report)
        for old_name, new_name in _DIR_ALIASES:
            _alias_directories(dst, old_name, new_name, True, report)
        report.rewritten_keys.extend(_rewrite_env_tree(dst))
        _rename_databases(dst, True, report)
        if report.write_errors or report.preview_incomplete:
            detail = "；".join(report.write_errors + report.preview_errors) or "改列失败"
            raise _ApplyFailure(detail)
        _clear_sidecars(dst)
        problem = _identity_problem(src, dst)
        if problem:
            raise _ApplyFailure(problem)
        guard.repair()
        if guard.problem:
            raise _ApplyFailure(f"来源文件和迁移前不一致：{guard.problem}")
        drift = _drift(src, source_hashes)
        if drift:
            raise _ApplyFailure(f"来源文件和迁移前不一致：{drift}")
        trust_info = carry_config_trust(project_root, src, dst)
        report.notes.append(str(trust_info.get("note") or ""))
        if trust_info.get("carried"):
            txn.trust_carried = True
            prior = trust_info.get("before")
            txn.trust_before = prior if isinstance(prior, dict) or prior is None else None
        _files, tables, _digests = _describe(dst, rewrite_env=False)
        file_hashes = {
            path.relative_to(dst).as_posix(): _sha256(path)
            for path in _walk_files(dst)
            if not _is_sqlite_sidecar(path.name)
        }
        payload = {
            "version": RECEIPT_VERSION,
            "status": "applied",
            "source": str(src),
            "dest": str(dst),
            "backup": str(backup),
            "moved_aside": report.moved_aside[0] if report.moved_aside else "",
            "column_renames": list(report.column_renames),
            "rewritten_keys": list(report.rewritten_keys),
            "files": file_hashes,
            "tables": tables,
            "applied_at": (now or datetime.now()).isoformat(timespec="seconds"),
            "config_trust": {
                "carried": bool(trust_info.get("carried")),
                "source_config": str(trust_info.get("source_config") or ""),
                "dest_config": str(trust_info.get("dest_config") or ""),
            },
        }
        receipt = dst / RECEIPT_NAME
        _write_text(receipt, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        text = _instructions(
            source=src, dest=dst, backup=str(backup),
            aside=report.moved_aside[0] if report.moved_aside else "",
            reason="迁移已完成。SQLite 用备份接口取了一致快照，WAL 已折进库文件，没有单独拷走 -wal/-shm。",
        )
        _write_text(dst / ROLLBACK_NAME, text)
        _write_text(backup / ROLLBACK_NAME, text)
        report.receipt = str(receipt)
        report.rollback_file = str(dst / ROLLBACK_NAME)
        report.notes.append("SQLite 用备份接口取一致快照，WAL 已折进库文件，没有把 -wal/-shm 单独拷走。来源字节保持原样。")
        _SHADOW_CACHE.pop(str(dst), None)
    except (_ApplyFailure, OSError) as exc:
        if txn.trust_carried:
            from ampuly.core.workspace_trust import WorkspaceTrustStore

            WorkspaceTrustStore().restore_workspace_entry(project_root, txn.trust_before)
            _delete_trust_backup(project_root)
            report.notes.append("信任记录：已把这次改过的记录退回迁移前。")
        _rollback_dest(dst, txn, report, str(exc), now)
    return report


def migrate_project_home(
    project_root: Path,
    *,
    apply: bool = False,
    source: Path | None = None,
    dest: Path | None = None,
    replace_target: bool = False,
    now: datetime | None = None,
    console_ports: tuple[int, ...] | None = None,
) -> HomeMigrationReport:
    """Plan (default) or copy ``.opc`` → ``.ampuly`` without modifying the source.

    ``apply=False`` lists the plan and writes nothing. ``apply=True`` refuses
    while the console lock or a console port is held, refuses symlinks and
    junctions, backs the source up with the SQLite backup API, and checks file
    hashes and table row counts before it writes a receipt.
    """
    root = Path(project_root)
    src = Path(source) if source is not None else root / LEGACY_DIR_NAME
    dst = Path(dest) if dest is not None else root / CURRENT_DIR_NAME
    report = HomeMigrationReport(source=src, dest=dst, apply=apply)
    if not _exists(src):
        if apply:
            return _refuse(report, "拒绝：旧目录不存在，没有迁移。未写入任何文件。")
        report.notes.append("旧目录不存在，无需迁移。")
        return report
    try:
        same = _absolute_key(src) == _absolute_key(dst)
    except OSError as exc:
        report.rejected = True
        report.notes.append(f"无法解析来源或目标路径：{exc}")
        return report
    if same:
        if apply:
            return _refuse(report, "拒绝：来源和目标是同一个目录，没有复制。未写入任何文件。")
        report.notes.append("来源和目标是同一个目录，没有复制。")
        return report
    if _is_link_or_junction(src) or _is_link_or_junction(dst):
        report.rejected = True
        report.notes.append("拒绝：来源或目标是符号链接/Junction；请给出真实目录后再迁移。")
        return report
    if _paths_nested(src, dst):
        report.rejected = True
        report.notes.append("拒绝：来源与目标存在嵌套关系，不能互相覆盖。")
        return report
    if apply:
        return _apply(
            report, src, dst,
            project_root=root,
            replace_target=replace_target,
            now=now,
            ports=_console_ports(console_ports),
        )

    # Dry-run only from here. Always include source in the read-only plan so a
    # pre-existing destination cannot hide pending source databases.
    _merge_tree(src, dst, False, report)
    scan_roots: list[Path] = [src]
    if _exists(dst):
        try:
            if _absolute_key(dst) != _absolute_key(src):
                scan_roots.append(dst)
                report.notes.append(
                    "目标目录已存在：预览同时只读扫描来源与目标；"
                    "这不是安全迁库批准，也未核验一致性备份/WAL。"
                )
        except OSError as exc:
            report.preview_incomplete = True
            report.preview_errors.append(f"预览未完成：无法解析目标路径 {dst}：{exc}")
    for scan in scan_roots:
        for old_name, new_name in _DIR_ALIASES:
            _alias_directories(scan, old_name, new_name, False, report)
        _rename_databases(scan, False, report)
    _note_longest_resulting(report, src, dst)
    report.notes.append(describe_config_trust(root, src, dst))
    return report


def _note_longest_resulting(report: HomeMigrationReport, src: Path, dst: Path) -> None:
    """Record the longest path apply would write, including the backup sibling."""
    try:
        rels = [
            item.relative_to(src).as_posix()
            for item in _walk_files(src)
            if not _is_sqlite_sidecar(item.name)
        ]
    except (OSError, ValueError) as exc:
        report.preview_incomplete = True
        report.preview_errors.append(f"预览未完成：无法测量路径长度：{exc}")
        return
    best = ""
    backup_root = src.parent / f"{src.name}.pre-migrate-{_STAMP_PLACEHOLDER}"
    for rel in rels:
        piece = Path(rel)
        for candidate in (dst / piece, backup_root / piece):
            text = str(candidate)
            if len(text) > len(best):
                best = text
    report.longest_resulting = best


def rollback_project_home(
    project_root: Path,
    *,
    source: Path | None = None,
    dest: Path | None = None,
    now: datetime | None = None,
    console_ports: tuple[int, ...] | None = None,
) -> HomeMigrationReport:
    """Park a migrated destination and put a moved-aside shadow back. Source stays."""
    root = Path(project_root)
    src = Path(source) if source is not None else root / LEGACY_DIR_NAME
    dst = Path(dest) if dest is not None else root / CURRENT_DIR_NAME
    report = HomeMigrationReport(source=src, dest=dst, apply=True)
    receipt_path = dst / RECEIPT_NAME
    if not _is_file_entry(receipt_path) or _is_link_or_junction(receipt_path):
        return _refuse(report, "拒绝：没有可退回的迁移回执。未改动任何目录。")
    try:
        data = json.loads(_read_text(receipt_path))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return _refuse(report, "拒绝：迁移回执读不出来。未改动任何目录。")
    if not isinstance(data, dict) or data.get("status") != "applied":
        return _refuse(report, "拒绝：迁移回执不是一次已完成的迁移。未改动任何目录。")
    if _receipt_claims_other_workspace(root, data):
        return _refuse(report, "拒绝：回执里的工作区不是这个项目，没有改信任库，也没有搬动目录。")
    aside_text = str(data.get("moved_aside") or "").strip()
    aside = Path(aside_text) if aside_text else None
    ports = _console_ports(console_ports)
    busy = _in_use_reason([src, dst, aside], ports)
    if busy:
        return _refuse(report, "拒绝：" + busy)
    if _is_link_or_junction(dst) or (aside is not None and _exists(aside) and _is_link_or_junction(aside)):
        return _refuse(report, "拒绝：目标或当初挪走的目录是符号链接/Junction。未改动任何目录。")
    if aside is not None and _exists(aside) and aside.parent != dst.parent:
        return _refuse(report, "拒绝：回执里挪走的目录不在目标旁边，没有自动搬动。")
    parked: Path | None = None
    try:
        parked = _sibling(dst, f"rolled-back-{_stamp(now)}")
        _rename(dst, parked)
        if aside is not None and _exists(aside):
            _rename(aside, dst)
    except OSError as exc:
        if parked is not None and _exists(parked) and not _exists(dst):
            try:
                _rename(parked, dst)
            except OSError:
                pass
        return _refuse(report, f"拒绝：退回失败：{exc}")
    report.moved_aside.append(str(parked))
    report.notes.append(undo_carried_trust(root, parked / "config"))
    text = _instructions(
        source=src, dest=dst, backup=str(data.get("backup") or ""), aside=str(parked),
        reason="已退回这次迁移。",
    )
    try:
        _write_text(parked / ROLLBACK_NAME, text)
        report.rollback_file = str(parked / ROLLBACK_NAME)
    except OSError:
        pass
    report.notes.append(text)
    _SHADOW_CACHE.pop(str(dst), None)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 .opc 数据目录复制到 .ampuly（默认只预览）")
    parser.add_argument("root", nargs="?", default=".", help="项目根目录")
    parser.add_argument("--apply", action="store_true", help="真的迁移。不加时只列出计划")
    parser.add_argument("--rollback", action="store_true", help="退回上一次成功的迁移（不删除来源）")
    parser.add_argument("--replace-target", action="store_true", help="目标不是空库影子时，仍把整个目标挪到旁边再迁移（不删除）")
    parser.add_argument("--from", dest="source", default="", help="旧数据目录")
    parser.add_argument("--to", dest="dest", default="", help="新数据目录")
    args = parser.parse_args(argv)
    if args.apply and args.rollback:
        print("不能同时加上 --apply 和 --rollback。", flush=True)
        return 1
    root = Path(args.root).expanduser().resolve()
    source = Path(args.source).expanduser() if args.source else None
    dest = Path(args.dest).expanduser() if args.dest else None
    if args.rollback:
        report = rollback_project_home(root, source=source, dest=dest)
    else:
        report = migrate_project_home(
            root,
            apply=bool(args.apply),
            source=source,
            dest=dest,
            replace_target=bool(args.replace_target),
        )
    for line in report.lines():
        print(line, flush=True)
    if not report.ok():
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
