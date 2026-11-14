"""运行库的表结构版本号，和「改表失败就整个退回去」（4.7-A19）。

业务库（``business.db``）和每个项目的运行库（``tasks.db``；默认项目没搬家时是 ``global.db``）以前都没有表结构
版本号：开机时各自 ``CREATE TABLE IF NOT EXISTS`` 再补几列，改到一半出错就停在一半——客户机上真出过一次，
没有回滚。

这里给它们一个编号（SQLite 自带的 ``PRAGMA user_version``）和一张有序的迁移列表。执行一次迁移：

1. 先拿到这个库的写锁，再在**同一目录**下拷一份带时间戳的备份（拿着写锁拷，拷到的就是改之前那一刻）；
2. 在**一个事务**里依次执行每一步，每做完一步把编号推到那一版；
3. 任何一步出错：撤回事务，核对版本号和表结构都回到了原样；事务被提前拆开、撤回失败或核对不上时才用备份
   恢复（有上限地等锁，等不到就明说没能确认），然后报一句中文——「失败在第几版、已退回」。

**只做到那条线**（owner 的规定：在现用运行库上执行迁移，要先确认范围）：

* 新建的库（文件原本不存在）建完表直接打上当前版本号——那是一个空库，没有东西可丢；
* 已经存在、还没有版本号的库，开机时**只读判断、不写**：记一行日志，数据页上显示「需要你确认后运行
  ``ampuly db-version --apply``」；
* 真正改动现有库的只有那条命令，而且控制台在用这些库时它拒绝执行。

每种库的迁移列表从「第 1 版 = 现状基线」起步，不改任何表，只给现状一个编号；以后的改动从这里往上数，每一次
都先备份、失败可退回。业务库第 2 版（4.7-A08）新增「分发授权单」一张表，不改旧表；第 3 版（S3-2A 项目中心）新增
「项目业务边界」和「项目接入规划」两张表，同样不改旧表。
"""

from __future__ import annotations

import re
import os
import stat
import sqlite3
import threading
import time
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from loguru import logger

BUSINESS = "business"
RUNTIME = "runtime"

# 给 owner 看的那条命令。日志里说的是它；数据页上再带上 ``--home``，见 :func:`owner_command`。
OWNER_COMMAND = "ampuly db-version --apply"

# How long a migration waits for another connection to let go of a database (BEGIN IMMEDIATE, COMMIT).
LOCK_WAIT_SECONDS = 5.0
# How long one backup/restore copy may keep waiting on a lock before it gives up. Python's
# ``Connection.backup()`` otherwise sleeps and retries on SQLITE_BUSY / SQLITE_LOCKED with no limit.
COPY_WAIT_SECONDS = 15.0
_BUSY_CODES = (5, 6)  # SQLITE_BUSY, SQLITE_LOCKED


def owner_command(home: str | Path) -> str:
    """数据页上给 owner 原样照抄的那条命令：写明是哪个数据目录，在哪个目录下运行都改的是这一份。"""
    return f'{OWNER_COMMAND} --home "{Path(home)}"'


class CopyTimedOut(RuntimeError):
    """A copy gave up because another connection kept the database locked past :data:`COPY_WAIT_SECONDS`."""


class _SourceSchemaError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if type(code) is str and code in ("invalid_source", "unavailable", "outcome_unknown") else "invalid_source"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True, repr=False)
class _DatabaseOpening:
    path: Path
    kind: str
    fd: int | None
    identity: tuple[int, int]


_LIVE_CREATIONS: dict[int, tuple[_DatabaseOpening, sqlite3.Connection | None]] = {}
_CREATION_LOCK = threading.RLock()
_SOURCE_SQL_LIMIT = 64 * 1024
_SOURCE_TOTAL_LIMIT = 512 * 1024


def _checked_database_path(value: str | Path) -> Path:
    if type(value) not in (str, type(Path("."))):
        raise _SourceSchemaError("invalid_source") from None
    path = Path(value).absolute()
    if (not path.name or ".." in path.parts or len(path.parts) > 16
            or path.anchor.startswith("\\\\") or str(path).startswith(("\\\\?\\", "\\\\.\\"))
            or any("\x00" in part for part in path.parts)
            or any(part.endswith((" ", ".")) or ":" in part
                   or part.split(".", 1)[0].upper() in
                   {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)},
                    *{f"LPT{i}" for i in range(1, 10)}} for part in path.parts[1:])):
        raise _SourceSchemaError("invalid_source") from None
    return path


def _path_identity(path: Path, *, allow_missing: bool = False):
    entries = (*reversed(path.parents), path)
    if len(entries) > 16:
        raise _SourceSchemaError("invalid_source") from None
    for entry in entries:
        try:
            info = entry.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise _SourceSchemaError("invalid_source") from None
        if entry == path:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _SourceSchemaError("invalid_source") from None
            return info
        if not stat.S_ISDIR(info.st_mode):
            raise _SourceSchemaError("invalid_source") from None
    return None


@contextmanager
def _database_opening(value: str | Path, kind: str, *, allow_create: bool = True):
    """Hold this call's exclusive-create evidence; never infer it from emptiness.

    This is initialization bookkeeping, not a source/actor execution capability.
    Ancestor and file checks are bounded best-effort checks, not a universal OS
    anti-race sandbox. No existing zero-byte file is adopted as newly created.
    """
    opening = None
    fd = None
    primary = None
    failure = None
    try:
        if type(kind) is not str or kind not in (BUSINESS, RUNTIME) or type(allow_create) is not bool:
            raise _SourceSchemaError("invalid_source")
        path = _checked_database_path(value)
        _path_identity(path, allow_missing=allow_create)
        if allow_create:
            path.parent.mkdir(parents=True, exist_ok=True)
            _path_identity(path, allow_missing=True)
            with _CREATION_LOCK:
                if len(_LIVE_CREATIONS) >= 32:
                    raise _SourceSchemaError("unavailable")
                try:
                    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
                except FileExistsError:
                    fd = None
                info = _path_identity(path)
                if fd is None and info.st_size == 0:
                    raise _SourceSchemaError("invalid_source")
                identity = (info.st_dev, info.st_ino)
                if fd is not None:
                    held = os.fstat(fd)
                    if (not stat.S_ISREG(held.st_mode) or held.st_nlink != 1 or held.st_size != 0
                            or (held.st_dev, held.st_ino) != identity):
                        raise _SourceSchemaError("invalid_source")
                opening = _DatabaseOpening(path, kind, fd, identity)
                if fd is not None:
                    _LIVE_CREATIONS[id(opening)] = (opening, None)
        else:
            info = _path_identity(path)
            if info.st_size == 0:
                raise _SourceSchemaError("invalid_source")
            opening = _DatabaseOpening(path, kind, None, (info.st_dev, info.st_ino))
    except _SourceSchemaError as error:
        failure = error.code
    except Exception:
        failure = "unavailable"
    except BaseException as error:
        primary = error
    if failure is None and primary is None:
        try:
            yield opening
        except BaseException as error:
            primary = error
    if opening is not None and opening.fd is not None:
        with _CREATION_LOCK:
            _LIVE_CREATIONS.pop(id(opening), None)
    if fd is not None:
        try:
            os.close(fd)
        except BaseException as error:
            if primary is None and failure is None:
                if isinstance(error, Exception):
                    failure = "outcome_unknown"
                else:
                    primary = error
    if primary is not None:
        raise primary
    if failure is not None:
        raise _SourceSchemaError(failure) from None


def _schema_rows(conn: sqlite3.Connection, sql: str, parameters=(), *, limit: int):
    cursor = conn.cursor()
    primary = None
    rows = None
    try:
        cursor.row_factory = None
        cursor.execute(sql, parameters)
        rows = cursor.fetchmany(limit + 1)
        if len(rows) > limit:
            raise _SourceSchemaError("invalid_source")
    except BaseException as error:
        primary = error
    try:
        cursor.close()
    except BaseException:
        if primary is None:
            raise
    if primary is not None:
        raise primary
    return rows


def _verify_database_opening(conn: sqlite3.Connection, opening: _DatabaseOpening) -> None:
    if type(conn) is not sqlite3.Connection or type(opening) is not _DatabaseOpening:
        raise _SourceSchemaError("invalid_source") from None
    info = _path_identity(opening.path)
    if (info.st_dev, info.st_ino) != opening.identity:
        raise _SourceSchemaError("outcome_unknown") from None
    rows = _schema_rows(conn, "PRAGMA database_list", limit=3)
    if any(type(row) is not tuple or len(row) != 3 or type(row[1]) is not str for row in rows):
        raise _SourceSchemaError("invalid_source") from None
    mains = [row for row in rows if row[1] == "main"]
    if (len(mains) != 1 or type(mains[0][2]) is not str
            or Path(mains[0][2]).absolute() != opening.path
            or any(row[1] not in ("main", "temp") for row in rows)):
        raise _SourceSchemaError("invalid_source") from None
    if opening.fd is not None:
        with _CREATION_LOCK:
            entry = _LIVE_CREATIONS.get(id(opening))
            if entry is None or entry[0] is not opening or entry[1] is not None and entry[1] is not conn:
                raise _SourceSchemaError("invalid_source") from None
            held = os.fstat(opening.fd)
            if held.st_nlink != 1 or (held.st_dev, held.st_ino) != opening.identity:
                raise _SourceSchemaError("outcome_unknown") from None
            _LIVE_CREATIONS[id(opening)] = (opening, conn)


def _stored_create_sql(sql: str) -> str:
    """Only SQLite's initial CREATE spelling; do not normalize any body token."""
    if type(sql) is not str:
        raise _SourceSchemaError("invalid_source") from None
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    match = re.match(
        r"\ACREATE\s+(TABLE|TRIGGER|UNIQUE\s+INDEX|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?",
        text, re.IGNORECASE,
    )
    if match is None:
        raise _SourceSchemaError("invalid_source") from None
    kind = match.group(1).upper()
    if kind.startswith("UNIQUE"):
        kind = "UNIQUE INDEX"
    return "CREATE " + kind + " " + text[match.end():]


def _source_statements(kind: str) -> tuple[str, ...]:
    # Delayed imports avoid Store -> schema_version -> Store and
    # BusinessStore -> schema_version -> priced-model -> BusinessStore cycles.
    if type(kind) is not str or kind not in (RUNTIME, BUSINESS):
        raise _SourceSchemaError("invalid_source") from None
    if kind == RUNTIME:
        from . import order_task_origin, order_run_origin, order_root_origin
        from . import order_root_task_binding, order_root_admission, controller_human_refusal
        return (
            *order_task_origin.SCHEMA_STATEMENTS, *order_run_origin.SCHEMA_STATEMENTS,
            *order_root_origin.SCHEMA_STATEMENTS, *order_root_task_binding.SCHEMA_STATEMENTS,
            *order_root_admission.SCHEMA_STATEMENTS, *controller_human_refusal.SCHEMA_STATEMENTS,
        )
    if kind == BUSINESS:
        from ..plugins.office_ui import launch_stop_intent, priced_model_reservation
        return (launch_stop_intent._TABLE_SQL, *launch_stop_intent._TRIGGERS.values(),
                *priced_model_reservation.SCHEMA_STATEMENTS)
    raise _SourceSchemaError("invalid_source") from None


def _source_objects(kind: str):
    statements = _source_statements(kind)
    if type(statements) is not tuple or len(statements) != (24 if kind == RUNTIME else 13):
        raise _SourceSchemaError("invalid_source") from None
    objects = {}
    tables = []
    for sql in statements:
        stored = _stored_create_sql(sql)
        match = re.match(r"CREATE (TABLE|TRIGGER|UNIQUE INDEX|INDEX) ([a-z][a-z0-9_]*)\b", stored)
        if match is None or match.group(2) in objects:
            raise _SourceSchemaError("invalid_source") from None
        category, name = match.groups()
        object_type = "index" if category in ("INDEX", "UNIQUE INDEX") else category.lower()
        if category == "TABLE":
            table = name
            tables.append(name)
        else:
            target = re.search(r"\bON ([a-z][a-z0-9_]*)\b", stored)
            if target is None:
                raise _SourceSchemaError("invalid_source") from None
            table = target.group(1)
        objects[name] = (object_type, table, stored)
    if len(tables) != (6 if kind == RUNTIME else 3):
        raise _SourceSchemaError("invalid_source") from None
    return statements, objects, tuple(tables)


def _source_schema_state(conn: sqlite3.Connection, kind: str) -> str:
    if type(conn) is not sqlite3.Connection:
        raise _SourceSchemaError("invalid_source") from None
    statements, expected, tables = _source_objects(kind)
    placeholders = ",".join("?" for _ in tables)
    rows = _schema_rows(conn,
        "SELECT type,name,tbl_name,length(CAST(sql AS BLOB)) FROM main.sqlite_schema "
        f"WHERE tbl_name IN ({placeholders}) ORDER BY name LIMIT 97", tables, limit=96)
    if not rows:
        return "absent"
    observed = set()
    total = 0
    for row in rows:
        if type(row) is not tuple or len(row) != 4 or any(type(value) is not str for value in row[:3]):
            raise _SourceSchemaError("invalid_source") from None
        object_type, name, table, length = row
        if name.startswith("sqlite_autoindex_"):
            if (object_type != "index" or length is not None
                    or re.fullmatch(r"sqlite_autoindex_" + re.escape(table) + r"_[1-9][0-9]?", name) is None):
                raise _SourceSchemaError("invalid_source") from None
            continue
        if (name not in expected or name in observed or type(length) is not int
                or not 1 <= length <= _SOURCE_SQL_LIMIT
                or expected[name][:2] != (object_type, table)):
            raise _SourceSchemaError("invalid_source") from None
        total += length
        if total > _SOURCE_TOTAL_LIMIT:
            raise _SourceSchemaError("invalid_source") from None
        raw = _schema_rows(conn,
            "SELECT length(CAST(sql AS BLOB)),substr(CAST(sql AS BLOB),1,?) "
            "FROM main.sqlite_schema WHERE type=? AND name=? LIMIT 2",
            (_SOURCE_SQL_LIMIT + 1, object_type, name), limit=1)
        if (len(raw) != 1 or type(raw[0]) is not tuple or len(raw[0]) != 2
                or type(raw[0][0]) is not int or raw[0][0] != length
                or type(raw[0][1]) is not bytes or len(raw[0][1]) != length):
            raise _SourceSchemaError("invalid_source") from None
        if _stored_create_sql(raw[0][1].decode("utf-8")) != expected[name][2]:
            raise _SourceSchemaError("invalid_source") from None
        observed.add(name)
    if observed != set(expected):
        raise _SourceSchemaError("invalid_source") from None
    return "complete"


def _add_source_bundle(conn: MigrationConnection, kind: str) -> None:
    if type(conn) is not MigrationConnection or type(conn._conn) is not sqlite3.Connection:
        raise _SourceSchemaError("invalid_source") from None
    state = _source_schema_state(conn._conn, kind)
    if state == "absent":
        for sql in _source_statements(kind):
            conn.execute(sql).close()
    if _source_schema_state(conn._conn, kind) != "complete":
        raise _SourceSchemaError("invalid_source") from None


def _add_runtime_source_bundle(conn: MigrationConnection) -> None:
    _add_source_bundle(conn, RUNTIME)


def _add_business_source_bundle(conn: MigrationConnection) -> None:
    _add_source_bundle(conn, BUSINESS)



def _add_runtime_browser_account_grant_structure(conn: MigrationConnection) -> None:
    """RUNTIME Migration 4: structure-only browser account grant tables.

    Shared-safe: no require_source_project. Does not seed catalog rows.
    Project grant writes remain on install_browser_account_grants (explicit).
    """
    from ampuly.database.browser_account_grants import install_browser_account_grant_structure
    install_browser_account_grant_structure(conn)


def _add_human_review_rework_receipts(conn: MigrationConnection) -> None:
    from .human_review_rework_schema import install_rework_receipts
    install_rework_receipts(conn)


BUSINESS_V6_CONSOLE_SESSIONS = (
    """CREATE TABLE IF NOT EXISTS console_sessions (
    token_sha256 TEXT PRIMARY KEY,
    login_key TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL,
    schema_version INTEGER NOT NULL,
    credential_fingerprint TEXT NOT NULL,
    workspace_binding TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS console_sessions_login_key ON console_sessions(login_key)",
)


def _console_session_schema_state(conn: sqlite3.Connection) -> str:
    """V6 is separate from the frozen v5 source bundle; partial is never repaired."""
    if type(conn) is not sqlite3.Connection:
        raise _SourceSchemaError("invalid_source") from None
    rows = _schema_rows(conn,
        "SELECT type,name,tbl_name,length(CAST(sql AS BLOB)),"
        "substr(CAST(sql AS BLOB),1,65537) FROM main.sqlite_schema "
        "WHERE tbl_name='console_sessions' OR name='console_sessions_login_key' LIMIT 5",
        limit=4)
    if not rows:
        return "absent"
    expected = {
        "console_sessions": ("table", "console_sessions", _stored_create_sql(BUSINESS_V6_CONSOLE_SESSIONS[0])),
        "console_sessions_login_key": ("index", "console_sessions", _stored_create_sql(BUSINESS_V6_CONSOLE_SESSIONS[1])),
        "sqlite_autoindex_console_sessions_1": ("index", "console_sessions", None),
    }
    observed = set()
    for row in rows:
        if type(row) is not tuple or len(row) != 5 or row[1] not in expected or row[1] in observed:
            raise _SourceSchemaError("invalid_source") from None
        object_type, name, table, length, raw = row
        if raw is None and length is None:
            normalized = None
        elif type(length) is int and 0 < length <= _SOURCE_SQL_LIMIT and type(raw) is bytes and len(raw) == length:
            try:
                normalized = _stored_create_sql(raw.decode("utf-8"))
            except UnicodeError:
                raise _SourceSchemaError("invalid_source") from None
        else:
            raise _SourceSchemaError("invalid_source") from None
        if (object_type, table, normalized) != expected[name]:
            raise _SourceSchemaError("invalid_source") from None
        observed.add(name)
    if observed != set(expected):
        raise _SourceSchemaError("invalid_source") from None
    try:
        from ..plugins.office_ui.console_session_store import schema
        schema(conn)
    except Exception:
        raise _SourceSchemaError("invalid_source") from None
    return "complete"


def _add_business_console_sessions(conn: MigrationConnection) -> None:
    if type(conn) is not MigrationConnection or type(conn._conn) is not sqlite3.Connection:
        raise _SourceSchemaError("invalid_source") from None
    if _source_schema_state(conn._conn, BUSINESS) != "complete":
        raise _SourceSchemaError("invalid_source") from None
    state = _console_session_schema_state(conn._conn)
    if state == "absent":
        for statement in BUSINESS_V6_CONSOLE_SESSIONS:
            conn.execute(statement).close()
    if _console_session_schema_state(conn._conn) != "complete":
        raise _SourceSchemaError("invalid_source") from None


def _current_schema_state(conn: sqlite3.Connection, kind: str) -> str:
    """Verify today's complete schema without changing the old v2 step."""
    state = _source_schema_state(conn, kind)
    session_state = _console_session_schema_state(conn) if kind == BUSINESS else "complete"
    if state != "complete":
        return state
    if session_state != "complete":
        return session_state
    if kind == RUNTIME:
        from .human_review_rework_schema import rework_receipt_schema_state
        if rework_receipt_schema_state(conn) != "complete":
            raise _SourceSchemaError("invalid_source") from None
        from ampuly.database.browser_account_grants import browser_account_grant_structure_state
        if browser_account_grant_structure_state(conn) != "complete":
            raise _SourceSchemaError("invalid_source") from None
    return "complete"


class MigrationConnection:
    """迁移步骤手里拿到的连接：只能在那个事务里执行语句，不能自己提交、撤回、开新事务，也不能改版本号。

    所有步骤必须落在同一个事务里，失败才能整个撤回。``sqlite3`` 的 ``executescript`` 会先偷偷提交一次，
    ``COMMIT`` / ``BEGIN`` / ``VACUUM`` 会把事务拆开，所以这里一概不给。版本号由执行器在每步之后推进。
    """

    _REFUSED = ("BEGIN", "COMMIT", "END", "VACUUM")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @staticmethod
    def _without_leading_comments(sql: str) -> str:
        """``/* x */ COMMIT`` and ``-- x\\nCOMMIT`` are still a COMMIT: skip comments before the first keyword."""
        text = str(sql or "")
        while True:
            text = text.lstrip()
            if text.startswith("--"):
                newline = text.find("\n")
                text = "" if newline < 0 else text[newline + 1:]
            elif text.startswith("/*"):
                end = text.find("*/", 2)
                text = "" if end < 0 else text[end + 2:]
            else:
                return text

    def _check(self, sql: str) -> None:
        text = self._without_leading_comments(sql)
        match = re.match(r"[A-Za-z_]+", text)
        head = match.group(0).upper() if match else ""
        upper = f" {text.upper()} "
        rollback_to = re.match(r"ROLLBACK\s+(TRANSACTION\s+)?TO\b", text, re.IGNORECASE) is not None
        if head in self._REFUSED or (head == "ROLLBACK" and not rollback_to):
            raise RuntimeError(f"a migration step may not run {head}: every step shares one transaction")
        if head == "PRAGMA" and "USER_VERSION" in upper:
            raise RuntimeError("a migration step may not set user_version: the runner does")

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        self._check(sql)
        return self._conn.execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        self._check(sql)
        return self._conn.executemany(sql, seq_of_parameters)


@dataclass(frozen=True)
class Migration:
    """一步改表：做完之后这个库就是第 ``version`` 版。``apply`` 只能用传进来的连接执行语句。

    **每一步都必须能在「已经做过这一步」的库上再做一遍而不出错**（``CREATE ... IF NOT EXISTS`` 式、只新增）：
    新建的业务库先按程序今天的建表语句建好，:func:`on_open` 再把登记过的每一步做一遍。一步 ``ALTER TABLE ... ADD
    COLUMN`` 如果那一列已经写进了建表语句，新库开机就会失败——这种步骤要先自己查一下那一列在不在。
    tests/test_db_schema_version.py 里有一条测试把每一步在新库上再做一遍，不满足这条的步骤会在那里变红。
    """

    version: int
    note_zh: str
    apply: Callable[[MigrationConnection], None]


def _baseline(_conn: MigrationConnection) -> None:
    """第 1 版就是今天的样子：不改任何表，只给现状一个编号。"""


# 业务库第 2 版（4.7-A08，批量自动化「每单一张分发授权单」，policy 批准建表）：只新增一张表和一个索引，
# 不改任何旧表。新建的库在开库时就建好（见 :func:`on_open`）；已经在用的库只有 owner 确认后运行命令才会加上。
BUSINESS_V2_DISTRIBUTION_AUTHORIZATIONS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS distribution_authorizations (
    authorization_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL DEFAULT 'default',
    order_id TEXT NOT NULL,
    batch_ref TEXT NOT NULL DEFAULT '',
    customer_id TEXT NOT NULL DEFAULT '',
    environment_id TEXT NOT NULL DEFAULT '',
    targets_json TEXT NOT NULL DEFAULT '[]',
    channels_json TEXT NOT NULL DEFAULT '[]',
    actions_json TEXT NOT NULL DEFAULT '[]',
    valid_days INTEGER NOT NULL DEFAULT 7,
    expires_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'revoked')),
    content_sha256 TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL DEFAULT '',
    revoked_by TEXT NOT NULL DEFAULT '',
    revoked_at TEXT NOT NULL DEFAULT '',
    revoke_reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_distribution_authorizations_order "
    "ON distribution_authorizations(project_id, order_id, created_at)",
)


def _add_distribution_authorizations(conn: MigrationConnection) -> None:
    """第 2 版：加上「分发授权单」表。只新增，不碰旧表和旧数据。"""
    for sql in BUSINESS_V2_DISTRIBUTION_AUTHORIZATIONS:
        conn.execute(sql)


# 业务库第 3 版（S3-2A 项目中心，policy 裁决）：项目的「业务边界」（从哪接手 / 范围 / 到哪结束 / 规划管理者）
# 和四条「接入规划」（业务来源 / 内部协作 / 客户确认 / 交付存储各一行：用什么软件、交接什么、怎么交互、挂在哪个阶段）。
# 只新增两张表，不改任何旧表。新建的库开库时就有；已经在用的库只有 owner 确认后运行命令才会加上。
# 列名 start_text / end_text：``end`` 是 SQL 关键字，不拿它做列名。
BUSINESS_V3_PROJECT_CENTER: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS project_profiles (
    project_id TEXT PRIMARY KEY,
    start_text TEXT NOT NULL DEFAULT '',
    scope_text TEXT NOT NULL DEFAULT '',
    end_text TEXT NOT NULL DEFAULT '',
    manager_employee_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
)""",
    """CREATE TABLE IF NOT EXISTS project_connection_plans (
    project_id TEXT NOT NULL,
    lane TEXT NOT NULL,
    tool TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, lane)
)""",
)


def _add_project_center_tables(conn: MigrationConnection) -> None:
    """第 3 版：加上「项目业务边界」和「项目接入规划」两张表。只新增，不碰旧表和旧数据。"""
    for sql in BUSINESS_V3_PROJECT_CENTER:
        conn.execute(sql)


# 业务库第 4 版（企业微信「微信客服」客户通道，policy「按推荐做」，runtime contract ）：四张新表——
# 哪个客服账号接哪个项目、哪条业务流（连同只属于 owner 的开单项和欢迎语）；每个客服账号拉消息拉到了哪里；每位客户一段
# 会话（阶段、问到的需求、工单、发送窗口、今天 AI 接待了几次）；进出的每条消息（按消息编号去重）。只新增，不改任何旧表。
# 新建的库开库时就有；已经在用的库只有 owner 确认后运行命令才会加上。
BUSINESS_V4_WECOM_KF: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS wecom_kf_accounts (
    open_kfid TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    account_name TEXT NOT NULL DEFAULT '',
    owner_only_fields_json TEXT NOT NULL DEFAULT '[]',
    welcome_text TEXT NOT NULL DEFAULT '',
    servicer_userid TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0,
    enabled_at INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS idx_wecom_kf_accounts_project ON wecom_kf_accounts(project_id)",
    """CREATE TABLE IF NOT EXISTS wecom_kf_cursors (
    open_kfid TEXT PRIMARY KEY,
    next_cursor TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS wecom_kf_conversations (
    conversation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    open_kfid TEXT NOT NULL,
    external_userid TEXT NOT NULL,
    workflow_id TEXT NOT NULL DEFAULT '',
    customer_id TEXT NOT NULL DEFAULT '',
    contact_id TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT 'greeting' CHECK (stage IN ('greeting', 'asking', 'ordered', 'handoff', 'closed')),
    collected_json TEXT NOT NULL DEFAULT '{}',
    order_id TEXT NOT NULL DEFAULT '',
    order_claimed_at TEXT NOT NULL DEFAULT '',
    last_customer_message_at INTEGER NOT NULL DEFAULT 0,
    sent_since_customer_message INTEGER NOT NULL DEFAULT 0,
    service_state INTEGER NOT NULL DEFAULT 0,
    ai_turns_day TEXT NOT NULL DEFAULT '',
    ai_turns_today INTEGER NOT NULL DEFAULT 0,
    handoff_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, open_kfid, external_userid)
)""",
    """CREATE TABLE IF NOT EXISTS wecom_kf_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    open_kfid TEXT NOT NULL,
    external_userid TEXT NOT NULL DEFAULT '',
    msgid TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    origin INTEGER NOT NULL DEFAULT 0,
    msgtype TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT '',
    send_time INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    text_cut INTEGER NOT NULL DEFAULT 0,
    media_id TEXT NOT NULL DEFAULT '',
    screen_json TEXT NOT NULL DEFAULT '[]',
    outcome TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL DEFAULT '',
    handled_at TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    UNIQUE (open_kfid, msgid)
)""",
    "CREATE INDEX IF NOT EXISTS idx_wecom_kf_messages_conversation ON wecom_kf_messages(conversation_id, id)",
)


def _add_wecom_kf_tables(conn: MigrationConnection) -> None:
    """第 4 版：加上企业微信客户通道的四张表。只新增，不碰旧表和旧数据。"""
    for sql in BUSINESS_V4_WECOM_KF:
        conn.execute(sql)


MIGRATIONS: dict[str, tuple[Migration, ...]] = {
    BUSINESS: (
        Migration(1, "现状基线（不改任何表）", _baseline),
        Migration(2, "新增「分发授权单」表（只新增，不改旧表）", _add_distribution_authorizations),
        Migration(3, "新增「项目业务边界」和「项目接入规划」两张表（只新增，不改旧表）", _add_project_center_tables),
        Migration(4, "新增企业微信客户通道的四张表：客服账号绑定、拉消息的位置、客户会话、进出消息（只新增，不改旧表）",
                  _add_wecom_kf_tables),
        Migration(5, "新增取消事实与报价预约保护表（无回填；旧库仅owner批准迁移）", _add_business_source_bundle),
        Migration(6, "新增控制台会话摘要与工作区绑定（无回填；旧库仅owner批准迁移）", _add_business_console_sessions),
    ),
    # B06-B2b/M4: do NOT register install_browser_account_grants as a RUNTIME Migration
    # (requires project identity). Migration 4 is structure-only via
    # install_browser_account_grant_structure — shared-safe CREATE, no catalog seed.
    # See the module contract.
    RUNTIME: (
        Migration(1, "现状基线（不改任何表）", _baseline),
        Migration(2, "新增原始创建、绑定、接纳及拒绝事实表（无回填；旧库仅owner批准迁移）", _add_runtime_source_bundle),
        Migration(3, "新增人审确认重派不可变回执（无回填；旧库仅owner批准迁移）", _add_human_review_rework_receipts),
        Migration(4, "新增浏览器账号授权目录表结构（只建表/触发器，无回填；shared 安全）",
                  _add_runtime_browser_account_grant_structure),
    ),
}


def runtime_migrations_exclude_project_grant_installer() -> bool:
    """B06-B2b lock: project grant installer must not appear as a RUNTIME migration step."""
    from ampuly.database import browser_account_grants as grants

    target = grants.install_browser_account_grants
    for step in MIGRATIONS[RUNTIME]:
        apply = step.apply
        if apply is target:
            return False
        code = getattr(apply, "__code__", None)
        if code is not None and "install_browser_account_grants" in code.co_names:
            return False
    return True


def current_version(kind: str) -> int:
    """这个程序认识的最新版本号。"""
    steps = MIGRATIONS[kind]
    return steps[-1].version if steps else 0


def stamp_sql(kind: str) -> str:
    """新建的库建完表后打上当前版本号用的那一句。PRAGMA 不接受参数，版本号是这里自己的整数。"""
    return f"PRAGMA user_version = {int(current_version(kind))}"


def is_fresh(path: str | Path) -> bool:
    """这个库是不是「原本不存在」：没有文件，或者是个零字节的空文件（SQLite 也把它当新库）。

    看不清楚（权限之类）就当它已经存在——那一边只读不写，是安全的那一边。
    """
    try:
        target = Path(path)
        return not target.exists() or target.stat().st_size == 0
    except OSError:
        return False


def _ro_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def read_version(path: str | Path) -> int | None:
    """只读地看一眼这个库的版本号。文件不在、打不开、不是库，都返回 ``None``——不瞎报一个数。"""
    target = Path(path)
    if not target.is_file():
        return None
    try:
        conn = sqlite3.connect(_ro_uri(target), uri=True)
    except (sqlite3.Error, OSError, ValueError):
        return None
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return int(row[0] or 0) if row else 0


def state_of(version: int | None, kind: str) -> str:
    """``current`` / ``unversioned`` / ``behind`` / ``newer`` / ``unreadable``。"""
    if version is None:
        return "unreadable"
    current = current_version(kind)
    if version == 0 and current > 0:
        return "unversioned"
    if version < current:
        return "behind"
    if version > current:
        return "newer"
    return "current"


_NOTED: set[tuple[str, int]] = set()
_NOTED_LOCK = threading.Lock()


def observe(path: str | Path, version: int, kind: str) -> str:
    """开库时读到一个已有库的版本号之后该说什么。**只记日志，不写库。**

    业务库每个请求都会开一次，所以同一个库、同一个版本号，每个进程只说一次。
    """
    state = state_of(version, kind)
    if state == "current":
        return state
    key = (str(path), int(version))
    with _NOTED_LOCK:
        if key in _NOTED:
            return state
        _NOTED.add(key)
    if state == "unversioned":
        logger.warning(
            "运行库还没有数据格式版本号，开机不会自动改它：{}。需要 owner 确认后运行 `{}` 打上版本号。",
            path, OWNER_COMMAND,
        )
    elif state == "behind":
        logger.warning(
            "运行库的数据格式是第 {} 版，这个程序认识到第 {} 版；开机不会自动升级它：{}。需要 owner 确认后运行 `{}`。",
            version, current_version(kind), path, OWNER_COMMAND,
        )
    elif state == "newer":
        logger.warning(
            "运行库的数据格式是第 {} 版，比这个程序认识的第 {} 版还新（可能装回了旧版程序），不动它：{}",
            version, current_version(kind), path,
        )
    return state


def on_open(conn: sqlite3.Connection, path: str | Path, kind: str, *, fresh: bool,
            _opening: _DatabaseOpening | None = None) -> int:
    """开库时那一步：新库打上当前版本号；已有的库只读版本号、记日志，**不写**。返回这个库现在的版本号。

    新库必须有本次 OS 独占新建且仍持有的原生文件证据；新 bundle 和版本同事务提交，失败不补齐。
    既有旧版本仅观察；声称当前版本的库必须有完整且精确的新 bundle。真正旧库迁移仍只由 owner 命令执行。
    """
    if fresh:
        failed = None
        primary = None
        commit_started = False
        try:
            if (type(_opening) is not _DatabaseOpening or _opening.fd is None
                    or _opening.kind != kind or _checked_database_path(path) != _opening.path):
                raise _SourceSchemaError("invalid_source")
            _verify_database_opening(conn, _opening)
            if conn.in_transaction:
                raise _SourceSchemaError("invalid_source")
            conn.execute("BEGIN IMMEDIATE").close()
            guarded = MigrationConnection(conn)
            for step in MIGRATIONS[kind]:
                step.apply(guarded)
                if not conn.in_transaction:
                    raise _SourceSchemaError("outcome_unknown")
            if _current_schema_state(conn, kind) != "complete":
                raise _SourceSchemaError("invalid_source")
            _verify_database_opening(conn, _opening)
            conn.execute(stamp_sql(kind)).close()
            commit_started = True
            conn.commit()
            if conn.in_transaction:
                raise _SourceSchemaError("outcome_unknown")
            _verify_database_opening(conn, _opening)
        except _SourceSchemaError as error:
            failed = "outcome_unknown" if commit_started else error.code
        except Exception:
            failed = "outcome_unknown" if commit_started else "unavailable"
        except BaseException as error:
            primary = error
        if failed is not None or primary is not None:
            try:
                if conn.in_transaction:
                    conn.rollback()
            except BaseException:
                # Keep the original failure; no retry or claim of rollback.
                pass
        if primary is not None:
            raise primary
        if failed is not None:
            raise _SourceSchemaError(failed) from None
        return current_version(kind)
    row = conn.execute("PRAGMA user_version").fetchone()
    version = int(row[0] or 0) if row else 0
    if kind == BUSINESS:
        # Old v5 may remain absent, but a partial/new alien auth bundle is
        # refused even before an explicit migration. Never add/repair here.
        _console_session_schema_state(conn)
    if version == current_version(kind) and _current_schema_state(conn, kind) != "complete":
        raise _SourceSchemaError("invalid_source") from None
    observe(path, version, kind)
    return version


def databases(home: str | Path) -> list[tuple[Path, str, str]]:
    """一个总目录下要管版本号的库 ``(路径, 种类, 给人看的名字)``：业务库、默认项目的运行库、每个项目自己的运行库。

    默认项目的运行库按 :func:`ampuly.database.store.default_runtime_db_path` 认（它看库里写下的身份，不看文件在
    不在），所以搬家后留作退路的旧 ``global.db`` 不在名单里——它已经不是在用的库了。只列存在的文件。
    """
    from ampuly.database.store import default_runtime_db_path

    root = Path(home)
    default_db = default_runtime_db_path(root)
    found: list[tuple[Path, str, str]] = [
        (root / "business.db", BUSINESS, "业务库"),
        (default_db, RUNTIME, "默认项目的运行库"),
    ]
    projects = root / "projects"
    if projects.is_dir():
        try:
            children = sorted(child for child in projects.iterdir() if child.is_dir())
        except OSError:
            children = []
        for child in children:
            db = child / "tasks.db"
            if db == default_db:
                continue
            if child.name == "default":
                # Not declared as the default project's store (default_runtime_db_path() kept global.db), so it is not
                # "project default's runtime DB". Still listed, so nothing is hidden -- just not mislabelled.
                found.append((db, RUNTIME, "默认项目目录下的一份运行库（没有登记为默认项目在用的那一份）"))
                continue
            found.append((db, RUNTIME, f"项目「{child.name}」的运行库"))
    return [row for row in found if row[0].is_file()]


def _named(version: int | None) -> str:
    if version is None:
        return "读不出版本号"
    return "没有版本号" if version == 0 else f"第 {version} 版"


def _state_text(version: int | None, kind: str) -> str:
    state = state_of(version, kind)
    current = current_version(kind)
    if state == "current":
        return f"第 {version} 版，是最新的"
    if state == "unversioned":
        return f"还没有版本号，要打上第 {current} 版"
    if state == "behind":
        return f"第 {version} 版，要升到第 {current} 版"
    if state == "newer":
        return f"第 {version} 版，比这个程序认识的第 {current} 版还新，不动它"
    return "读不出版本号"


def health(home: str | Path) -> dict[str, Any]:
    """数据页上的那一句：每个库在第几版，以及要不要 owner 动手。只读，一个字节都不写。"""
    rows: list[dict[str, Any]] = []
    for path, kind, label in databases(home):
        version = read_version(path)
        rows.append({
            "name": path.name, "label": label, "path": str(path), "kind": kind,
            "version": version, "current": current_version(kind), "state": state_of(version, kind),
        })
    count = {state: sum(1 for row in rows if row["state"] == state)
             for state in ("unversioned", "behind", "newer", "unreadable")}
    needs = count["unversioned"] + count["behind"]
    said: list[str] = []
    if count["unversioned"]:
        said.append(f"有 {count['unversioned']} 个库还没有数据格式版本号")
    if count["behind"]:
        said.append(f"有 {count['behind']} 个库的数据格式比这个程序旧")
    sentences: list[str] = []
    if said:
        sentences.append("，".join(said) + "。系统不会自己去改它们：等你确认之后再改，改之前每个库都会先自动备份，出错会自动退回。")
    if count["newer"]:
        sentences.append(f"有 {count['newer']} 个库的数据格式比这个程序还新（可能装回了旧版程序），系统不会动它们。")
    if count["unreadable"]:
        sentences.append(f"有 {count['unreadable']} 个库读不出版本号。")
    if not rows:
        why = "没有读到任何运行库。"
    elif sentences:
        why = "".join(sentences)
    else:
        why = "所有运行库的数据格式版本号都是最新的。"
    return {"databases": rows, "needs_owner": bool(needs), "command": owner_command(home), "why_zh": why}


@dataclass
class MigrationResult:
    path: str
    ok: bool
    before: int | None
    after: int | None
    target: int
    failed_at: int | None = None
    backup: str = ""
    restored: bool = False
    error: str = ""
    message_zh: str = ""


def _plan(migrations: Sequence[Migration], before: int, target: int) -> list[Migration]:
    versions = [int(step.version) for step in migrations]
    if versions != sorted(set(versions)) or any(version < 1 for version in versions):
        raise ValueError(f"migration versions must be distinct, ascending and start from 1: {versions}")
    return [step for step in migrations if before < step.version <= target]


def _backup_path(path: Path, target: int, now: datetime) -> Path:
    base = f"{path.name}.before-v{int(target)}-{now.strftime('%Y%m%d-%H%M%S')}"
    candidate = path.with_name(base + ".bak")
    serial = 2
    while candidate.exists():
        candidate = path.with_name(f"{base}-{serial}.bak")
        serial += 1
    return candidate


def _copy(source: Path, target: Path, *, wait_seconds: float | None = None) -> None:
    """SQLite 自己的在线备份接口：拷到的一定是一份完整、一致的库，不会是写了一半的那一份。

    **有上限地等。** ``Connection.backup()`` 碰到别的连接占着锁（SQLITE_BUSY / SQLITE_LOCKED）会一直睡一下再试，
    没有尽头；这里在进度回调里看钟，超过 ``wait_seconds``（默认 :data:`COPY_WAIT_SECONDS`）就抛
    :class:`CopyTimedOut`。回调一抛，SQLite 结束这次备份并撤回目标库上没做完的写入，目标不会只被盖了一半。
    """
    limit = COPY_WAIT_SECONDS if wait_seconds is None else float(wait_seconds)
    deadline = time.monotonic() + limit
    lock_wait = max(0.0, min(limit, 2.0))

    def _give_up_when_stuck(status: int, _remaining: int, _total: int) -> None:
        if status in _BUSY_CODES and time.monotonic() >= deadline:
            raise CopyTimedOut(f"{target} stayed locked by another connection for {limit:g}s; copy abandoned")

    src = sqlite3.connect(str(source), timeout=lock_wait)
    try:
        dst = sqlite3.connect(str(target), timeout=lock_wait)
        try:
            src.backup(dst, progress=_give_up_when_stuck)
        finally:
            dst.close()
    finally:
        src.close()


def _schema(path: Path) -> list[tuple[str, str, str]] | None:
    try:
        conn = sqlite3.connect(_ro_uri(path), uri=True)
    except (sqlite3.Error, OSError, ValueError):
        return None
    try:
        return [(str(row[0]), str(row[1]), str(row[2] or "")) for row in conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name")]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def migrate(path: str | Path, kind: str | None = None, *, migrations: Sequence[Migration] | None = None,
            target: int | None = None, now: datetime | None = None) -> MigrationResult:
    """把一个**已经存在**的库升到 ``target`` 版（默认：这个程序认识的最新版）。不会新建库。

    调用方负责先确认没有别的程序正在写这个库（命令行在控制台运行时拒绝执行）。

    失败时的退路分两层：事务还完整、``ROLLBACK`` 成功，库就已经一个字节不差地回到了改之前——再核对一遍版本号和
    表结构，对得上就**不再**拿备份去盖（盖一次只会多出一段「别人刚写进去的东西被悄悄盖掉」的空档，还可能
    被别的读者卡住）。只有事务被提前结束、撤回失败或者核对不上时，才用备份恢复；那一步有上限地等，等不到就
    明说「没能确认已经恢复」，不会无声地卡住。
    """
    target_path = Path(path)
    steps_all: tuple[Migration, ...] = tuple(migrations) if migrations is not None else MIGRATIONS[str(kind)]
    goal = int(target) if target is not None else (steps_all[-1].version if steps_all else 0)
    before = read_version(target_path)
    result = MigrationResult(path=str(target_path), ok=False, before=before, after=before, target=goal)
    if before is None:
        result.message_zh = "找不到或读不出这个库，没有改动。"
        return result
    if before > goal:
        result.ok = True
        result.message_zh = f"这个库是{_named(before)}，比这个程序认识的第 {goal} 版还新，不动它。"
        return result
    steps = _plan(steps_all, before, goal)
    if not steps:
        if migrations is None and kind == BUSINESS and before == goal == 6:
            # A v6 stamp is not evidence that its auth bundle is complete.
            # This no-op path observes only: no backup, repair or stamping.
            check = None
            verified = False
            try:
                with _database_opening(target_path, BUSINESS, allow_create=False) as opening:
                    check = sqlite3.connect(_ro_uri(opening.path), uri=True,
                                            timeout=0.0, isolation_level=None)
                    check.execute("PRAGMA query_only=ON").close()
                    check.execute("BEGIN").close()
                    _verify_database_opening(check, opening)
                    verified = _current_schema_state(check, BUSINESS) == "complete"
                    _verify_database_opening(check, opening)
            except Exception:
                verified = False
            finally:
                if check is not None:
                    try:
                        check.rollback()
                    except Exception:
                        verified = False
                    try:
                        check.close()
                    except Exception:
                        verified = False
            if not verified:
                result.error = "current_schema_unverified"
                result.message_zh = "当前第6版结构校验未通过，没有修改。"
                return result
        result.ok = True
        result.message_zh = f"已经是{_named(before)}，不用改。"
        return result

    backup = _backup_path(target_path, goal, now or datetime.now())
    conn = sqlite3.connect(str(target_path), timeout=LOCK_WAIT_SECONDS, isolation_level=None)
    began = False
    running: int | None = None
    try:
        # 先拿写锁再拷备份：从这一刻到提交，别人写不进来，备份就是「改之前」那一刻的库。
        conn.execute("BEGIN IMMEDIATE")
        began = True
        _copy(target_path, backup)
        result.backup = str(backup)
        guarded = MigrationConnection(conn)
        for step in steps:
            running = step.version
            step.apply(guarded)
            if not conn.in_transaction:
                # Whatever got past MigrationConnection, the shared transaction is gone: stop here and say so.
                raise RuntimeError(f"migration step {step.version} ended the shared transaction")
            conn.execute(f"PRAGMA user_version = {int(step.version)}")
        running = None
        conn.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001 - every failure takes the same way back
        result.error = f"{type(exc).__name__}: {exc}"
        result.failed_at = running
        # A clean ROLLBACK of a transaction that is still whole puts back every byte; anything else
        # (the transaction ended early, the rollback itself failed) means only the backup can be trusted.
        rolled_back = False
        if began:
            intact = conn.in_transaction
            try:
                conn.execute("ROLLBACK")
                rolled_back = intact
            except sqlite3.Error:
                rolled_back = False
        conn.close()
        if not began:
            result.message_zh = "这个库正被别的程序占着，没有改动。"
            logger.warning("schema migration not started for {}: {}", target_path, result.error)
            return result
        if not result.backup:
            result.message_zh = "改之前的备份没做成，所以一处都没改。"
            logger.warning("schema migration aborted before any change for {}: {}", target_path, result.error)
            return result
        schema_before = _schema(backup)

        def _as_before() -> bool:
            after = read_version(target_path)
            result.after = after
            return schema_before is not None and after == before and _schema(target_path) == schema_before

        restore_error = ""
        copied = False
        if not (rolled_back and _as_before()):
            try:
                restore_source = backup
                if kind == BUSINESS:
                    # A whole-file restore must never resurrect an older login.
                    # Keep the original backup intact and restore only a new,
                    # independently checked copy with its sessions removed.
                    from .console_session_restore import sanitize_business_restore_copy
                    restore_source = backup.with_name(
                        backup.name + ".console-sessions-cleared-" + os.urandom(8).hex()
                    )
                    sanitize_business_restore_copy(backup, restore_source)
                _copy(restore_source, target_path)
                copied = True
            except Exception as restore_exc:  # noqa: BLE001 - reported below, the backup stays where it is
                restore_error = f"{type(restore_exc).__name__}: {restore_exc}"
            result.restored = not restore_error and _as_before()
        else:
            result.restored = True
        where = f"改到第 {running} 版时出错" if running is not None else "最后保存时出错"
        if "locked" in result.error.lower() or "busy" in result.error.lower():
            where += "（这个库正被别的程序开着）"
        if result.restored:
            how = "并用改之前的备份恢复过" if copied else "库和改之前一模一样"
            if copied and kind == BUSINESS:
                how += "，控制台会话已失效，需要重新登录"
            result.message_zh = f"{where}，已经退回{_named(before)}：改动全部撤回，{how}。备份留在 {backup}"
        else:
            result.message_zh = (f"{where}；没能确认这个库已经回到改之前的样子（可能有别的程序正开着它）。"
                                 f"改之前的备份原样留在 {backup}，先别再动这个库，关掉别的程序后把这句话转给维护的人。")
        logger.warning("schema migration failed for {} ({}); rolled_back={} copied={} restored={} {}: {}",
                       target_path, where, rolled_back, copied, result.restored, restore_error, result.error)
        return result
    conn.close()
    result.after = read_version(target_path)
    result.ok = result.after == steps[-1].version
    result.message_zh = f"改好了：从{_named(before)}升到{_named(result.after)}。改之前的备份在 {backup}"
    logger.info("schema migration done for {}: {} -> {} (backup {})", target_path, before, result.after, backup)
    return result


def migrate_home(home: str | Path, *, apply: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """命令行 ``ampuly db-version`` 的全部内容。不加 ``apply`` 只列，一个字节都不写；加了才对落后的库逐个迁移。

    调用方负责先确认控制台没在用这些库（命令行会检查锁和端口），这里不再猜。
    """
    rows = databases(home)
    if not rows:
        return {"ok": True, "lines": [f"这个目录下没有找到运行库：{home}"], "results": []}
    lines: list[str] = []
    results: list[MigrationResult] = []
    ok = True
    pending = 0
    for path, kind, label in rows:
        version = read_version(path)
        state = state_of(version, kind)
        if apply and state in ("unversioned", "behind"):
            outcome = migrate(path, kind, now=now)
            results.append(outcome)
            ok = ok and outcome.ok
            lines.append(f"{label}（{path}）：{outcome.message_zh}")
            continue
        if state in ("unversioned", "behind"):
            pending += 1
        if state == "unreadable":
            ok = False
        lines.append(f"{label}：{_state_text(version, kind)}（{path}）")
    if not apply and pending:
        lines.append(f"只列不改：上面有 {pending} 个库要打版本号，确认后加 --apply 才会真的去改。"
                     "改之前每个库都会先在同一目录下自动备份，出错会自动退回。")
    return {"ok": ok, "lines": lines, "results": results}
