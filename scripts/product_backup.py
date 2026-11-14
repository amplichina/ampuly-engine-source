"""Verified product-state backup and clean-target restore.

This is deliberately separate from ``backup_runtime.py`` (rolling database
snapshots) and project archives (one project's portable files).  A product
backup is the disaster-recovery unit for one SuperOnes installation: database
truth, operator-owned configuration, installed workflow/component assets and
customer workplaces.

The archive never contains browser profiles, logs, caches, prior backups or
credential values.  Restore only accepts absent target roots, so it cannot
silently merge stale and recovered state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
import sys
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ampuly.core import scheduled_task  # noqa: E402
from ampuly.database.console_session_restore import clear_console_sessions, ConsoleSessionRestoreError  # noqa: E402


SCHEMA = "superones-product-backup/1"
MANIFEST = "manifest.json"
DATABASES = ("business.db", "global.db", "ui_state.db", "channel_inbound.db")
# 每个项目自己的任务库，以及它写一半时留下的两个伴生文件。这三样**不能**当普通文件拷：引擎在跑的时候
# 拷出来的 .db 可能是撕裂的，而 -wal 里的改动和 .db 对不上——恢复出来是一份打得开、但内容不自洽的库。
# 它们改走在线备份 API，和总目录根下那四个库同样待遇。
LIVE_PROJECT_DATABASE_FILES = ("tasks.db", "tasks.db-wal", "tasks.db-shm")
HOME_ROOTS = (
    "config",
    "workflows",
    "skills",
    "prompts",
    "projects",
    "memory",
    "connector_candidates",
    "improvement_proposals",
    "skill_activations",
    "skill_candidates",
    "employee_packages",
)
# Publication facts are stored beside the versioned employee files.  They must
# be SQLite snapshots too, never a raw .db/WAL copy hidden in the asset loop.
ASSET_DATABASES = ("employee_packages/.lifecycle/authority.db",)
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
MAX_CAPTURE_FILES = 100_000
MAX_CAPTURE_DATABASES = 256
EXCLUDED_RUNTIME_STATE = (
    "agent_homes",
    "artifacts",
    "backups",
    "bin",
    "browser",
    "builder_trials",
    "component_trials",
    "computer_use",
    "exports",
    "logs",
    "node_trials",
    "run",
)
_INLINE_SECRET = re.compile(
    r"^\s*(?:[-\"']?)(api_key|token|password|secret|app_secret|verification_token|"
    r"bridge_token|client_secret|imap_password|smtp_password|bot_token|app_token|"
    r"access_token|private_key)(?:[\"']?)\s*[:=]\s*(.+?)\s*$",
    re.IGNORECASE,
)



def _replace(source: Path, target: Path) -> None:
    """``os.replace`` that outlasts a scanner's grip on files that were written a moment ago.

    On Windows an antivirus or the indexer holds a fresh file for a few milliseconds and the rename over the final
    name is refused with WinError 5. Here that turned a customer's upgrade, rollback or restore into a random
    "access denied" -- the full gate met it once in fifteen runs. Only that refusal is retried, a bounded number of
    times; anything else, and the last refusal, is raised as it is. Kept in this file on purpose: the script runs
    before the product's own packages can be imported.
    """
    for attempt in range(6):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


class BackupRefused(RuntimeError):
    """The requested backup/restore would violate the recovery contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_nonempty_secret(value: str) -> bool:
    clean = value.strip().rstrip(",").strip().strip('"\'')
    return bool(clean) and clean.lower() not in {"null", "none", "~", "{}", "[]"}


def _assert_no_inline_secret(path: Path) -> None:
    if path.suffix.lower() not in {".yaml", ".yml", ".json", ".toml", ".ini", ".conf"}:
        return
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return
    for line_no, line in enumerate(text.splitlines(), 1):
        match = _INLINE_SECRET.match(line)
        if match and not match.group(1).lower().endswith("_env") and _is_nonempty_secret(match.group(2)):
            raise BackupRefused(
                f"inline credential-like value in {path.name}:{line_no}; move it to an environment reference first"
            )


def _safe_files(root: Path) -> Iterable[Path]:
    _assert_not_link(root)
    if not root.exists():
        return
    pending = [root]
    visited = 0
    while pending:
        folder = pending.pop()
        _assert_not_link(folder)
        for path in sorted(folder.iterdir()):
            # Check before descending: Windows junctions are not symlinks.
            _assert_not_link(path)
            visited += 1
            if visited > MAX_CAPTURE_FILES:
                raise BackupRefused("backup inventory exceeds its file/directory limit")
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                try:
                    path.resolve().relative_to(root.resolve())
                except ValueError as exc:
                    raise BackupRefused(f"file escapes backup root: {path}") from exc
                yield path
            else:
                raise BackupRefused(f"irregular backup source: {path}")


def _assert_not_link(path: Path) -> None:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise BackupRefused(f"symbolic links and junctions are not backed up: {path}")


def _source_stamp(path: Path) -> tuple[int, ...]:
    _assert_not_link(path)
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _assert_source_path(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    _assert_not_link(current)
    for part in relative.parts:
        current = current / part
        _assert_not_link(current)


def _database_sources(home: Path) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for relative in (*DATABASES, *ASSET_DATABASES):
        path = home / relative
        _assert_source_path(path, home)
        if path.is_file():
            sources[f"home/{relative}"] = path
    projects = home / "projects"
    _assert_not_link(projects)
    if projects.exists():
        for folder in sorted(projects.iterdir()):
            _assert_not_link(folder)
            if folder.is_dir():
                path = folder / "tasks.db"
                _assert_not_link(path)
                if path.is_file():
                    sources[f"home/projects/{folder.name}/tasks.db"] = path
    if len(sources) > MAX_CAPTURE_DATABASES:
        raise BackupRefused("backup exceeds its database limit")
    return dict(sorted(sources.items()))


def _asset_sources(home: Path, workplace: Path, databases: dict[str, Path]) -> dict[str, tuple[Path, str]]:
    excluded = {path for path in databases.values()}
    excluded.update(Path(str(path) + suffix) for path in databases.values() for suffix in SQLITE_SIDECARS)
    sources: dict[str, tuple[Path, str]] = {}
    for name in HOME_ROOTS:
        for path in _safe_files(home / name):
            if path not in excluded:
                sources[f"home/{path.relative_to(home).as_posix()}"] = (path, "runtime_asset")
    for path in _safe_files(workplace):
        sources[f"workplace/{path.relative_to(workplace).as_posix()}"] = (path, "customer_workplace")
    if len(sources) > MAX_CAPTURE_FILES:
        raise BackupRefused("backup exceeds its file limit")
    return dict(sorted(sources.items()))


@contextmanager
def _native_asset_capture_locks(assets):
    """Windows mandatory share guards for the entire captured asset set.

    Hold all existing file identities against write and replacement while every
    SQLite writer fence is held. New/removed paths are separately rejected by
    inventory validation. This is a captured-content cut, not a volume snapshot.
    Other platforms keep the explicitly weaker revalidation protocol.
    """
    if os.name != "nt":
        yield False
        return
    import ctypes
    from ctypes import wintypes
    if len(assets) > 4096:
        raise BackupRefused("native file fence exceeds 4096 assets; split the installation before backup")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handles = []
    primary = None
    try:
        for path, _ in assets.values():
            # GENERIC_READ / FILE_SHARE_READ only: writes and deletes from any
            # process conflict, including writers which ignore advisory locks.
            _assert_not_link(path)
            handle = create(str(path), 0x80000000, 0x1, None, 3, 0x00200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise BackupRefused("asset is busy; native capture did not start")
            handles.append(handle)
            _assert_not_link(path)
        yield True
    except BaseException as exc:
        primary = exc
        raise
    finally:
        failed = False
        for handle in reversed(handles):
            if not close(handle): failed = True
        if failed and primary is None:
            raise BackupRefused("asset fence release could not be confirmed")


@contextmanager
def _database_capture_locks(sources: dict[str, Path]):
    """Hold every SQLite writer lock together, or refuse without waiting.

    Snapshots use separate read connections: backing up a connection while it
    owns BEGIN IMMEDIATE can wait forever. No row/schema write is performed.
    All locks remain held through source validation and archive publication.
    """
    with ExitStack() as stack:
        identities: dict[str, tuple[int, int]] = {}
        for member, path in sources.items():
            stamp = _source_stamp(path)
            try:
                connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0)
                stack.callback(connection.close)
                connection.execute("PRAGMA busy_timeout=0")
                connection.execute("BEGIN IMMEDIATE")
                stack.callback(connection.rollback)
            except sqlite3.Error as exc:
                raise BackupRefused(f"database is busy or cannot be frozen: {member}") from exc
            if _source_stamp(path)[:2] != stamp[:2]:
                raise BackupRefused(f"database identity changed during capture: {member}")
            identities[member] = stamp[:2]
        yield identities


def _stage_asset(source: Path, target: Path) -> tuple[int, ...]:
    before = _source_stamp(source)
    _assert_no_inline_secret(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        opened = os.fstat(incoming.fileno())
        if (opened.st_dev, opened.st_ino) != before[:2]:
            raise BackupRefused("asset identity changed before capture")
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
    if _source_stamp(source) != before:
        raise BackupRefused("asset changed during capture")
    _assert_no_inline_secret(target)
    return before


def _validate_capture(
    *, home: Path, workplace: Path, databases: dict[str, Path],
    database_identities: dict[str, tuple[int, int]],
    assets: dict[str, tuple[Path, str]], asset_stamps: dict[str, tuple[int, ...]],
    staged_records: list[dict[str, object]],
) -> None:
    if _database_sources(home) != databases or _asset_sources(home, workplace, databases) != assets:
        raise BackupRefused("backup source inventory changed during capture")
    for member, path in databases.items():
        if _source_stamp(path)[:2] != database_identities[member]:
            raise BackupRefused(f"database identity changed during capture: {member}")
    hashes = {str(item["path"]): str(item["sha256"]) for item in staged_records}
    for member, (path, _kind) in assets.items():
        before = _source_stamp(path)
        if before != asset_stamps[member] or _sha256(path) != hashes[member] or _source_stamp(path) != before:
            raise BackupRefused(f"asset changed during capture: {member}")


def _snapshot_database(source: Path, target: Path) -> None:
    source_db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=0)
    try:
        target_db = sqlite3.connect(str(target))
        try:
            deadline = time.monotonic() + 30

            def progress(status, remaining, total):
                if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() > deadline:
                    raise BackupRefused(f"database snapshot busy or timed out: {source.name}")

            source_db.backup(target_db, pages=256, progress=progress, sleep=0)
        finally:
            target_db.close()
    finally:
        source_db.close()
    check = sqlite3.connect(str(target))
    try:
        result = str(check.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        check.close()
    if result != "ok":
        raise BackupRefused(f"database snapshot failed integrity check: {source.name}: {result}")


def _clear_business_snapshot_sessions(path: Path) -> int:
    """Only a private captured/restored copy is modified, before activation."""
    connection = None
    try:
        connection = sqlite3.connect(str(path), timeout=0, isolation_level=None)
        if connection.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise BackupRefused("console session snapshot unavailable")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            cleared = clear_console_sessions(connection)
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise BackupRefused("console session snapshot integrity failed")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        return cleared
    except ConsoleSessionRestoreError as exc:
        raise BackupRefused("console session snapshot refused: " + exc.code) from None
    except Exception:
        raise BackupRefused("console session snapshot unavailable") from None
    finally:
        if connection is not None:
            connection.close()


def _record(path: Path, member: str, kind: str) -> dict[str, object]:
    return {"path": member, "kind": kind, "size": path.stat().st_size, "sha256": _sha256(path)}


def create_backup(*, home: Path, workplace: Path, archive: Path) -> dict[str, object]:
    _assert_not_link(home)
    _assert_not_link(workplace)
    home = home.resolve()
    workplace = workplace.resolve()
    archive = archive.resolve()
    if not home.is_dir():
        raise BackupRefused(f"runtime home does not exist: {home}")
    if not workplace.is_dir():
        raise BackupRefused(f"workplace root does not exist: {workplace}")
    if archive.exists():
        raise BackupRefused(f"archive already exists: {archive}")
    if archive.is_relative_to(home) or archive.is_relative_to(workplace):
        raise BackupRefused("backup archive must be outside the state it protects")

    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="superones-product-backup-", dir=str(archive.parent)) as raw:
        stage = Path(raw)
        databases = _database_sources(home)
        if not databases:
            raise BackupRefused("no authoritative database was found")
        with _database_capture_locks(databases) as identities:
            # Assets are frozen into staging, not merely hashed now and read
            # again from mutable live paths while writing the ZIP later.
            assets = _asset_sources(home, workplace, databases)
            with _native_asset_capture_locks(assets) as native_files:
                stamps: dict[str, tuple[int, ...]] = {}
                records: list[dict[str, object]] = []
                for member, (source, kind) in assets.items():
                    target = stage / member
                    stamps[member] = _stage_asset(source, target)
                    records.append(_record(target, member, kind))
                for member, source in databases.items():
                    target = stage / member
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _snapshot_database(source, target)
                    if member == "home/business.db":
                        _clear_business_snapshot_sessions(target)
                    records.append(_record(target, member, "database"))
                records.sort(key=lambda item: str(item["path"]))

                def validate_sources():
                    _validate_capture(home=home, workplace=workplace, databases=databases,
                                      database_identities=identities, assets=assets,
                                      asset_stamps=stamps, staged_records=records)

                validate_sources()
                manifest: dict[str, object] = {
                    "schema": SCHEMA,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "files": records,
                    "total_bytes": sum(int(item["size"]) for item in records),
                    "excluded_runtime_state": list(EXCLUDED_RUNTIME_STATE),
                    "credentials_included": False,
                    "active_console_sessions_included": False,
                    "restore_requires_secret_reprovisioning": True,
                    "capture": {
                        "protocol": "sqlite+native-file-fence/2" if native_files else "sqlite-writer-fence+staged-files/1",
                        "captured_files_common_lock_window": native_files,
                        "database_common_lock_window": True,
                        "database_count": len(databases),
                        "files_staged_and_revalidated": True,
                        # A user/editor can mutate files without the product's
                        # SQLite locks. Detected drift refuses this backup; this
                        # is not a filesystem/volume snapshot of arbitrary writers.
                        "filesystem_atomic_snapshot": False,
                        "external_file_writers_must_be_quiescent": not native_files,
                    },
                }
                body = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                manifest["content_digest"] = hashlib.sha256(body).hexdigest()
                temporary = archive.with_name(f".{archive.name}.{os.getpid()}.tmp")
                try:
                    with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as output:
                        output.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
                        for item in records:
                            member = str(item["path"])
                            output.write(stage / member, member)
                    validate_sources()
                    if archive.exists():
                        raise BackupRefused("backup archive appeared during capture; refusing overwrite")
                    _replace(temporary, archive)
                    try:
                        return verify_backup(archive)
                    except Exception:
                        archive.unlink(missing_ok=True)
                        raise
                finally:
                    temporary.unlink(missing_ok=True)


def _load_and_verify(archive: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    if not archive.is_file():
        raise BackupRefused(f"backup archive does not exist: {archive}")
    try:
        with zipfile.ZipFile(archive) as source:
            names = source.namelist()
            if not names or names[0] != MANIFEST or len(names) != len(set(names)):
                raise BackupRefused("archive manifest is missing, duplicated, or not first")
            manifest = json.loads(source.read(MANIFEST).decode("utf-8"))
            if manifest.get("schema") != SCHEMA:
                raise BackupRefused(f"unsupported backup schema: {manifest.get('schema')!r}")
            claimed = str(manifest.pop("content_digest", ""))
            actual = hashlib.sha256(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            manifest["content_digest"] = claimed
            if claimed != actual:
                raise BackupRefused("manifest digest does not match")
            raw_records = manifest.get("files")
            if not isinstance(raw_records, list) or not all(isinstance(item, dict) for item in raw_records):
                raise BackupRefused("manifest files must be a list of records")
            records = list(raw_records)
            expected = [MANIFEST, *[str(item.get("path", "")) for item in records]]
            if names != expected:
                raise BackupRefused("archive members do not match the manifest")
            total = 0
            for item in records:
                member = str(item.get("path", ""))
                if item.get("kind") not in {"database", "runtime_asset", "customer_workplace"}:
                    raise BackupRefused(f"unsupported backup member kind: {item.get('kind')!r}")
                path = Path(member)
                parts = member.split("/")
                if (
                    path.is_absolute()
                    or "\\" in member
                    or "\x00" in member
                    or any(not part or ":" in part for part in parts)
                    or ".." in parts
                    or not member.startswith(("home/", "workplace/"))
                ):
                    raise BackupRefused(f"unsafe archive member: {member}")
                info = source.getinfo(member)
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise BackupRefused(f"symbolic link in archive: {member}")
                digest = hashlib.sha256()
                with source.open(info) as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                if info.file_size != int(item.get("size", -1)) or digest.hexdigest() != item.get("sha256"):
                    raise BackupRefused(f"archived content does not match manifest: {member}")
                total += info.file_size
            if total != int(manifest.get("total_bytes", -1)):
                raise BackupRefused("archive byte total does not match manifest")
            if manifest.get("credentials_included") is not False:
                raise BackupRefused("archive does not attest that credential values are excluded")
            return manifest, records
    except (OSError, TypeError, ValueError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        if isinstance(exc, BackupRefused):
            raise
        raise BackupRefused(f"backup archive is unreadable: {exc}") from exc


def verify_backup(archive: Path) -> dict[str, object]:
    manifest, records = _load_and_verify(archive.resolve())
    return {
        "ok": True,
        "schema": manifest["schema"],
        "files": len(records),
        "bytes": manifest["total_bytes"],
        "credentials_included": manifest["credentials_included"],
        "active_console_sessions_included": manifest.get("active_console_sessions_included"),
        "restore_requires_secret_reprovisioning": manifest["restore_requires_secret_reprovisioning"],
        "capture": manifest.get("capture", {"protocol": "legacy-unfenced"}),
    }


def restore_backup(*, archive: Path, target_home: Path, target_workplace: Path) -> dict[str, object]:
    archive = archive.resolve()
    target_home = target_home.resolve()
    target_workplace = target_workplace.resolve()
    for target in (target_home, target_workplace):
        if target.exists():
            raise BackupRefused(f"restore target already exists; refusing to merge or overwrite: {target}")
    _, records = _load_and_verify(archive)
    target_home.parent.mkdir(parents=True, exist_ok=True)
    target_workplace.parent.mkdir(parents=True, exist_ok=True)
    staging_home = target_home.with_name(f".{target_home.name}.restore-{os.getpid()}")
    staging_workplace = target_workplace.with_name(f".{target_workplace.name}.restore-{os.getpid()}")
    if staging_home.exists() or staging_workplace.exists():
        raise BackupRefused("restore staging path already exists")
    home_activated = False
    try:
        staging_home.mkdir()
        staging_workplace.mkdir()
        with zipfile.ZipFile(archive) as source:
            for item in records:
                member = str(item["path"])
                prefix, relative = member.split("/", 1)
                root = staging_home if prefix == "home" else staging_workplace
                destination = (root / relative).resolve()
                try:
                    destination.relative_to(root.resolve())
                except ValueError as exc:
                    raise BackupRefused(f"archive member escapes restore target: {member}") from exc
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as incoming, destination.open("xb") as output:
                    shutil.copyfileobj(incoming, output, length=1024 * 1024)
        # 按清单里标着「database」的那些来查，不按写死的四个名字来查：项目的任务库也是库，恢复出来同样
        # 要能打开、要完整。只查那四个的话，项目库坏了也照样宣布恢复成功。
        for item in records:
            if item.get("kind") != "database":
                continue
            member = str(item["path"])
            if not member.startswith("home/"):
                continue
            db = staging_home / member.split("/", 1)[1]
            if not db.is_file():
                continue
            connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                    raise BackupRefused(f"restored database failed integrity check: {member}")
            finally:
                connection.close()
        restored_business = staging_home / "business.db"
        if restored_business.is_file():
            _clear_business_snapshot_sessions(restored_business)
        _replace(staging_home, target_home)
        home_activated = True
        try:
            _replace(staging_workplace, target_workplace)
        except Exception:
            # The target did not exist before this call.  Put the first half
            # back into staging so the common cleanup below removes it; never
            # expose a half-restored installation.
            _replace(target_home, staging_home)
            home_activated = False
            raise
    except Exception:
        if home_activated and target_home.exists():
            _replace(target_home, staging_home)
        shutil.rmtree(staging_home, ignore_errors=True)
        shutil.rmtree(staging_workplace, ignore_errors=True)
        raise
    return {
        "ok": True,
        "home": str(target_home),
        "workplace": str(target_workplace),
        "files": len(records),
        "credentials_restored": False,
        "active_console_sessions_restored": False,
        "next_step": "re-provision credential environment references before starting the service",
    }


ARCHIVE_PREFIX = "product-"
WEEKLY_TASK_NAME = "AmpulyEngineWeeklyArchive"
RECEIPT_NAME = "last-archive.json"


def weekly_archive(home: Path, workplace: Path, dest: Path) -> int:
    """做一份整机归档，名字自己起，然后留一张能核对的回执。

    此归档还包括操作者自建的提示词、技能和配置，不能仅用数据库备份替代。

    它不每天做：一份归档把整个工作区装进去，每天做等于每天复制一遍全部交付物。周一次，按份数封顶。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    archive = Path(dest) / f"{ARCHIVE_PREFIX}{stamp}.zip"
    note = ""
    made: dict[str, object] = {}
    try:
        made = create_backup(home=Path(home), workplace=Path(workplace), archive=archive)
        verify_backup(archive)
        ok = True
        note = f"{made.get('files', 0)} 个文件，检查正常"
    except Exception as exc:  # noqa: BLE001 - 失败也要留痕，否则失败会被藏起来
        ok = False
        note = f"{type(exc).__name__}: {exc}"
    receipt = Path(home) / "backups" / "auto" / RECEIPT_NAME
    try:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({
            "at": datetime.now().isoformat(timespec="seconds"),
            "home": str(Path(home).resolve()),
            "workplace": str(Path(workplace).resolve()),
            "archive": str(archive) if ok else "",
            "ok": ok,
            "note": note,
            "files": int(made.get("files", 0) or 0),
            "bytes": int(made.get("bytes", 0) or 0),
        }, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass
    print(("整机归档完成：" if ok else "整机归档失败：") + note)
    if ok:
        print(f"  {archive}")
        print(f"  它保护的是：{Path(home).resolve()}")
    return 0 if ok else 1


def install_weekly_task(home: Path, workplace: Path, dest: Path, *, name: str = WEEKLY_TASK_NAME) -> int:
    """每周日 03:30 做一份整机归档。避开日备的 03:00，别让两件事同时读同一批库。"""
    arguments = (f'"{Path(__file__).resolve()}" auto'
                 f' --home "{Path(home).resolve()}" --workplace "{Path(workplace).resolve()}"'
                 f' --dest "{Path(dest).resolve()}"')
    try:
        code, said = scheduled_task.install(
            name=name, execute=sys.executable, arguments=arguments,
            schedule="WEEKLY", at="03:30", day="Sunday",
            expect_in_arguments=str(Path(home).resolve()))
    except scheduled_task.NotWindows:
        print("只在 Windows 上注册计划任务；Linux 上用 cron：")
        print(f"  30 3 * * 0 {sys.executable} {arguments}")
        return 2
    for line in said:
        print(line)
    if code == 0:
        print(f"  它保护的是：{Path(home).resolve()}")
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="Verified SuperOnes product-state backup and clean restore")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--home", required=True)
    create.add_argument("--workplace", required=True)
    create.add_argument("--archive", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("archive")
    restore = sub.add_parser("restore")
    restore.add_argument("archive")
    restore.add_argument("--target-home", required=True)
    restore.add_argument("--target-workplace", required=True)
    auto = sub.add_parser("auto", help="做一份整机归档（名字自己起）并留回执")
    auto.add_argument("--home", required=True)
    auto.add_argument("--workplace", required=True)
    auto.add_argument("--dest", required=True)
    weekly = sub.add_parser("install-task", help="注册每周日 03:30 的整机归档任务")
    weekly.add_argument("--home", required=True)
    weekly.add_argument("--workplace", required=True)
    weekly.add_argument("--dest", required=True)
    weekly.add_argument("--name", default=WEEKLY_TASK_NAME)
    args = parser.parse_args()
    if args.command == "auto":
        return weekly_archive(Path(args.home), Path(args.workplace), Path(args.dest))
    if args.command == "install-task":
        return install_weekly_task(Path(args.home), Path(args.workplace), Path(args.dest), name=args.name)
    try:
        if args.command == "create":
            result = create_backup(home=Path(args.home), workplace=Path(args.workplace), archive=Path(args.archive))
        elif args.command == "verify":
            result = verify_backup(Path(args.archive))
        else:
            result = restore_backup(
                archive=Path(args.archive), target_home=Path(args.target_home),
                target_workplace=Path(args.target_workplace),
            )
    except BackupRefused as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
