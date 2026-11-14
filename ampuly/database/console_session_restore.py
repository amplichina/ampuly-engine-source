"""Invalidate console sessions only as part of an explicitly requested full restore.

No AccountStore/BusinessStore is created and no schema is migrated. A project
record import must not call these helpers. Source backups stay byte-for-byte
unchanged; a restored database never exposes a previously live console session.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat
import time

COPY_SECONDS = 30.0
MAX_COPY_BYTES = 512 * 1024 * 1024
MAX_SESSION_ROWS = 100_000
MAX_SCHEMA_TABLES = 256
_SIDECARS = ('-wal', '-shm', '-journal')
_COLUMNS = (
    (0, 'token_sha256', 'TEXT', 0, None, 1, 0),
    (1, 'login_key', 'TEXT', 1, None, 0, 0),
    (2, 'issued_at', 'REAL', 1, None, 0, 0),
    (3, 'expires_at', 'REAL', 1, None, 0, 0),
    (4, 'revoked_at', 'REAL', 0, None, 0, 0),
    (5, 'schema_version', 'INTEGER', 1, None, 0, 0),
    (6, 'credential_fingerprint', 'TEXT', 1, None, 0, 0),
    (7, 'workspace_binding', 'TEXT', 1, None, 0, 0),
)
_CODES = frozenset({
    'invalid_source', 'invalid_target', 'target_exists', 'invalid_session_schema',
    'session_trigger_refused', 'session_reference_refused', 'session_limit',
    'source_changed', 'target_changed', 'source_sidecar_refused', 'source_limit',
    'busy', 'timeout', 'integrity_failed', 'unavailable',
})


class ConsoleSessionRestoreError(ValueError):
    """A fixed public code, never an account, credential, path or SQL detail."""
    def __init__(self, code: str):
        self.code = code if type(code) is str and code in _CODES else 'unavailable'
        super().__init__(self.code)


def _fail(code):
    raise ConsoleSessionRestoreError(code) from None


@contextmanager
def _boundary():
    code = None
    try:
        yield
    except ConsoleSessionRestoreError as exc:
        code = exc.code
    except Exception as exc:
        code = ('busy' if type(exc) is sqlite3.OperationalError
                and getattr(exc, 'sqlite_errorcode', None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                else 'unavailable')
    if code is not None:
        raise ConsoleSessionRestoreError(code) from None


def _rows(conn, sql, parameters=(), *, maximum):
    cursor = sqlite3.Connection.cursor(conn)
    try:
        cursor.row_factory = None
        cursor.execute(sql, parameters)
        rows = cursor.fetchmany(maximum + 1)
        if len(rows) > maximum:
            _fail('invalid_session_schema')
        return rows
    finally:
        cursor.close()


def _session_schema(conn):
    rows = _rows(conn, "SELECT type,name,tbl_name FROM main.sqlite_schema "
                 "WHERE name='console_sessions' COLLATE NOCASE", maximum=1)
    if not rows:
        return False
    if rows != [('table', 'console_sessions', 'console_sessions')]:
        _fail('invalid_session_schema')
    if tuple(_rows(conn, "PRAGMA main.table_xinfo('console_sessions')", maximum=8)) != _COLUMNS:
        _fail('invalid_session_schema')
    indexes = _rows(conn, "PRAGMA main.index_list('console_sessions')", maximum=2)
    if (len(indexes) != 2 or {row[1:] for row in indexes} != {
        ('sqlite_autoindex_console_sessions_1', 1, 'pk', 0),
        ('console_sessions_login_key', 0, 'c', 0),
    }):
        _fail('invalid_session_schema')
    for name, column, ordinal in (
        ('sqlite_autoindex_console_sessions_1', 'token_sha256', 0),
        ('console_sessions_login_key', 'login_key', 1),
    ):
        if _rows(conn, f"PRAGMA main.index_info('{name}')", maximum=1) != [(0, ordinal, column)]:
            _fail('invalid_session_schema')
    for schema in ('main', 'temp'):
        if _rows(conn, f"SELECT 1 FROM {schema}.sqlite_schema WHERE type='trigger' "
                 "AND tbl_name='console_sessions' COLLATE NOCASE LIMIT 1", maximum=1):
            _fail('session_trigger_refused')
    if _rows(conn, "PRAGMA main.foreign_key_list('console_sessions')", maximum=1):
        _fail('invalid_session_schema')
    # An incoming ON DELETE CASCADE could otherwise mutate a different table.
    tables = _rows(conn, "SELECT name FROM main.sqlite_schema WHERE type='table' LIMIT 257", maximum=MAX_SCHEMA_TABLES)
    for (name,) in tables:
        if type(name) is not str or not name or len(name) > 256 or any(ord(ch) < 32 for ch in name):
            _fail('invalid_session_schema')
        quoted = '"' + name.replace('"', '""') + '"'
        for row in _rows(conn, f'PRAGMA main.foreign_key_list({quoted})', maximum=128):
            if len(row) < 3 or type(row[2]) is not str:
                _fail('invalid_session_schema')
            if row[2].lower() == 'console_sessions':
                _fail('session_reference_refused')
    return True


def clear_console_sessions(conn: sqlite3.Connection) -> int:
    """Clear known session rows in the caller's active transaction; never commit.

    Caller must roll back/refuse activation on any exception. Legacy databases
    with no session table are accepted without creating a table or an epoch.
    """
    with _boundary():
        if type(conn) is not sqlite3.Connection or not conn.in_transaction:
            _fail('invalid_source')
        if not _session_schema(conn):
            return 0
        count = _rows(conn, 'SELECT count(*) FROM (SELECT 1 FROM main.console_sessions LIMIT 100001)', maximum=1)[0][0]
        if type(count) is not int or count > MAX_SESSION_ROWS:
            _fail('session_limit')
        prior = _rows(conn, 'PRAGMA main.secure_delete', maximum=1)[0][0]
        if type(prior) is not int or prior not in (0, 1, 2):
            _fail('invalid_source')
        try:
            conn.execute('PRAGMA main.secure_delete=ON')
            cursor = conn.execute('DELETE FROM main.console_sessions')
            try:
                if cursor.rowcount != count:
                    _fail('unavailable')
            finally:
                cursor.close()
        finally:
            conn.execute(f'PRAGMA main.secure_delete={prior}')
        if _rows(conn, 'SELECT count(*) FROM main.console_sessions', maximum=1) != [(0,)]:
            _fail('unavailable')
        return count


def _path(value, *, exists):
    if type(value) is not type(Path('.')):
        _fail('invalid_source' if exists else 'invalid_target')
    if value.drive and not value.root:
        _fail('invalid_source' if exists else 'invalid_target')
    path = value.absolute()
    if ('..' in path.parts or len(path.parts) > 24 or path.anchor.startswith('\\\\')
            or any('\x00' in part for part in path.parts)
            or any(':' in part or part.endswith((' ', '.')) for part in path.parts[1:])):
        _fail('invalid_source' if exists else 'invalid_target')
    entries = (*reversed(path.parents), path)
    for item in entries:
        try:
            info = item.lstat()
        except FileNotFoundError:
            if item == path and not exists:
                return path, None
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            _fail('invalid_source' if exists else 'invalid_target')
        if item != path:
            if not stat.S_ISDIR(info.st_mode):
                _fail('invalid_source' if exists else 'invalid_target')
        elif not exists:
            _fail('target_exists')
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _fail('invalid_source')
    return path, info


def _identity(info):
    return info.st_dev, info.st_ino


def _stamp(info):
    return _identity(info), info.st_size, info.st_mtime_ns


def _no_sidecars(path):
    for suffix in _SIDECARS:
        try:
            path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        _fail('source_sidecar_refused')


def sanitize_business_restore_copy(source: Path, target: Path) -> dict[str, object]:
    """Make one new, integrity-checked restore source with no console sessions.

    Input is a fixed, quiescent backup/staged database, never a live database.
    Failures leave any newly created output unactivated for caller diagnosis.
    No retry, service start, migration, source mutation or path logging occurs.
    """
    with _boundary():
        source, before = _path(source, exists=True)
        target, _ = _path(target, exists=False)
        if source == target or before.st_size <= 0 or before.st_size > MAX_COPY_BYTES:
            _fail('source_limit')
        _no_sidecars(source)
        _no_sidecars(target)
        deadline = time.monotonic() + COPY_SECONDS
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, 'O_BINARY', 0), 0o600)
        owned = os.fstat(fd)
        src = dst = None
        timed_out = False
        try:
            src = sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=0)
            dst = sqlite3.connect(str(target), timeout=0, isolation_level=None)
            page_size = src.execute('PRAGMA page_size').fetchone()[0]
            if type(page_size) is not int or not 512 <= page_size <= 65536:
                _fail('invalid_source')
            def progress(status, _remaining, total):
                if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    _fail('busy')
                if time.monotonic() > deadline:
                    _fail('timeout')
                if total * page_size > MAX_COPY_BYTES:
                    _fail('source_limit')
            src.backup(dst, pages=256, progress=progress, sleep=0)
            def guard():
                nonlocal timed_out
                timed_out = time.monotonic() > deadline
                return int(timed_out)
            dst.set_progress_handler(guard, 1000)
            if dst.execute('PRAGMA journal_mode=DELETE').fetchone() != ('delete',):
                _fail('unavailable')
            dst.execute('PRAGMA trusted_schema=OFF')
            dst.execute('BEGIN IMMEDIATE')
            try:
                cleared = clear_console_sessions(dst)
                if dst.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                    _fail('integrity_failed')
                dst.execute('COMMIT')
            except BaseException:
                if dst.in_transaction:
                    dst.execute('ROLLBACK')
                raise
        except Exception:
            if timed_out:
                _fail('timeout')
            raise
        finally:
            try:
                if src is not None:
                    src.close()
                if dst is not None:
                    dst.close()
            finally:
                os.close(fd)
        current, after = _path(source, exists=True)
        if current != source or _stamp(before) != _stamp(after):
            _fail('source_changed')
        _, output = _path(target, exists=True)
        if _identity(owned) != _identity(output):
            _fail('target_changed')
        _no_sidecars(source)
        _no_sidecars(target)
        return {'schema': 'ampuly.console-session-restore/1',
                'console_sessions_cleared': cleared,
                'active_console_sessions_included': False}
