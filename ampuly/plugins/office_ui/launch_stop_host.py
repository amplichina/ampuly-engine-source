"""Own one existing native Stop transaction; never run or stop a worker.

The filesystem checks are the narrowly copied B62 rules, not a directory-handle
lease. DELETE and WAL are retained; SQLite's normal WAL/SHM lifecycle is allowed.
The original owner/writer perform the SQL observations. No schema is installed.
"""
from __future__ import annotations

from datetime import datetime
import math
import os
from pathlib import Path
import sqlite3
import stat

from .accounts import AccountStore
from .business_store import BusinessStore
from .current_account_principal import _login
from .current_owner_rw import (
    CurrentOwnerObservationError, _OwnerFault, _now, _source_path, _token,
    _observe_current_owner_on_rw_connection,
)
from .employee_order_launch_anchor import _ReadBudget
from .launch_stop_source import LaunchStopSourceHeaders
from .launch_stop_intent import (
    LaunchStopIntentError, LaunchStopIntentFact, _error_code, _headers, _reason,
    request_launch_stop_on_connection,
)


_MISSING = object()
_SOURCE_FIELDS = (
    'project_id', 'order_id', 'outbox_id', 'workflow_id', 'workflow_version',
    'launch_payload_sha256', 'claim_event_id', 'event_chain_head_sha256',
)
_CODES = ('invalid_source', 'unavailable', 'forbidden', 'conflict', 'outcome_unknown')


class _HostFault(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise _HostFault(code)


class _CapturedSource:
    """Private references only; never returned, logged or used as a permit."""
    __slots__ = (
        'business', 'accounts', 'business_fields', 'account_fields', 'raw_path',
        'raw_home', 'cwd', 'path', 'home', 'tokens', 'token', 'entry', 'expires',
        'source', 'source_fields', 'source_values',
    )

    def __repr__(self) -> str:
        return '<private Stop source>'


def _capture(business, accounts, token, source, reason) -> _CapturedSource:
    if type(business) is not BusinessStore or type(accounts) is not AccountStore:
        _fail('invalid_source')
    business_fields = object.__getattribute__(business, '__dict__')
    account_fields = object.__getattribute__(accounts, '__dict__')
    if (type(business_fields) is not dict or type(account_fields) is not dict
            or account_fields.get('business') is not business):
        _fail('invalid_source')
    token = _token(token)
    if type(source) is not LaunchStopSourceHeaders:
        _fail('invalid_source')
    source_fields = object.__getattribute__(source, '__dict__')
    if (type(source_fields) is not dict or len(source_fields) != len(_SOURCE_FIELDS)
            or any(type(key) is not str for key in source_fields)
            or set(source_fields) != set(_SOURCE_FIELDS)):
        _fail('invalid_source')
    values = _headers(source)
    _reason(reason)
    tokens = account_fields.get('_tokens')
    if type(tokens) is not dict:
        _fail('invalid_source')
    entry = tokens.get(token, _MISSING)
    if entry is _MISSING:
        _fail('forbidden')
    if type(entry) is not tuple or len(entry) != 2:
        _fail('invalid_source')
    login_key = _login(entry[0])
    expires = entry[1]
    if (login_key.lower() != login_key or type(expires) not in (int, float)
            or expires <= 0 or type(expires) is int and expires > 2**63 - 1
            or type(expires) is float and not math.isfinite(expires)):
        _fail('invalid_source')
    cwd = Path.cwd().absolute()
    raw_path = business_fields.get('path')
    raw_home = account_fields.get('home')
    path = _source_path(raw_path, cwd)
    home = _source_path(raw_home, cwd)
    if path.name != 'business.db' or path != home / 'business.db':
        _fail('invalid_source')
    captured = _CapturedSource()
    captured.business = business
    captured.accounts = accounts
    captured.business_fields = business_fields
    captured.account_fields = account_fields
    captured.raw_path = raw_path
    captured.raw_home = raw_home
    captured.cwd = cwd
    captured.path = path
    captured.home = home
    captured.tokens = tokens
    captured.token = token
    captured.entry = entry
    captured.expires = expires
    captured.source = source
    captured.source_fields = source_fields
    captured.source_values = values
    _source_unchanged(captured)
    return captured


def _source_unchanged(captured: _CapturedSource) -> None:
    if (type(captured.business) is not BusinessStore or type(captured.accounts) is not AccountStore
            or object.__getattribute__(captured.business, '__dict__') is not captured.business_fields
            or object.__getattribute__(captured.accounts, '__dict__') is not captured.account_fields
            or captured.account_fields.get('business') is not captured.business
            or captured.business_fields.get('path') is not captured.raw_path
            or captured.account_fields.get('home') is not captured.raw_home
            or Path.cwd().absolute() != captured.cwd
            or _source_path(captured.raw_path, captured.cwd) != captured.path
            or _source_path(captured.raw_home, captured.cwd) != captured.home):
        _fail('invalid_source')
    if (captured.account_fields.get('_tokens') is not captured.tokens
            or captured.tokens.get(captured.token, _MISSING) is not captured.entry):
        _fail('unavailable')
    if _now() > captured.expires:
        _fail('forbidden')
    if (type(captured.source) is not LaunchStopSourceHeaders
            or object.__getattribute__(captured.source, '__dict__') is not captured.source_fields
            or len(captured.source_fields) != len(_SOURCE_FIELDS)
            or any(type(key) is not str for key in captured.source_fields)
            or set(captured.source_fields) != set(_SOURCE_FIELDS)
            or _headers(captured.source) != captured.source_values):
        _fail('invalid_source')


def _file_snapshot(path: Path) -> tuple[int, int, int]:
    """B62 file_snapshot: normal local path, <=16 ancestors, no link/reparse."""
    parts = path.parts
    if (not path.is_absolute() or path.drive.startswith('\\\\')
            or path.name != 'business.db' or not 1 <= len(parts) <= 16):
        _fail('invalid_source')
    current = type(path)(parts[0])
    info = None
    for index in range(len(parts)):
        if index:
            current = current / parts[index]
        info = current.lstat()
        attributes = getattr(info, 'st_file_attributes', 0)
        if stat.S_ISLNK(info.st_mode) or attributes & 0x400:
            _fail('invalid_source')
        if index < len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                _fail('invalid_source')
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _fail('invalid_source')
    return (info.st_dev, info.st_ino, info.st_nlink)


def _safe_sidecars(path: Path, *, _allow_owned_journal: bool = False) -> None:
    """B62 guards; only this open write's regular single-link journal is allowed."""
    for suffix in ('-wal', '-shm', '-journal'):
        try:
            info = path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        if suffix == '-journal' and not _allow_owned_journal:
            _fail('unavailable')
        attributes = getattr(info, 'st_file_attributes', 0)
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or attributes & 0x400 or info.st_nlink != 1):
            _fail('invalid_source')


def _database_header(path: Path, identity: tuple[int, int, int]) -> tuple[int, int]:
    """B62 bounded native header read, preserving a read's primary on close."""
    stream = None
    header = None
    primary = None
    try:
        stream = path.open('rb')
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_dev, info.st_ino, info.st_nlink) != identity):
            _fail('invalid_source')
        header = stream.read(20)
    except BaseException as exc:
        primary = exc
    finally:
        if stream is not None:
            try:
                stream.close()
            except BaseException as exc:
                if primary is None:
                    primary = exc
    if primary is not None:
        raise primary
    if (type(header) is not bytes or len(header) != 20
            or header[:16] != b'SQLite format 3\x00'):
        _fail('invalid_source')
    versions = (header[18], header[19])
    if versions not in ((1, 1), (2, 2)):
        _fail('unavailable')
    if _file_snapshot(path) != identity:
        _fail('invalid_source')
    return versions


def _sql_rows(conn: sqlite3.Connection, sql: str, maximum: int = 1) -> list[tuple]:
    cursor = None
    primary = None
    rows = None
    try:
        cursor = sqlite3.Connection.execute(conn, sql)
        rows = sqlite3.Cursor.fetchmany(cursor, maximum + 1)
    except BaseException as exc:
        primary = exc
    finally:
        if cursor is not None:
            try:
                sqlite3.Cursor.close(cursor)
            except BaseException as exc:
                if primary is None:
                    primary = exc
    if primary is not None:
        raise primary
    if (type(rows) is not list or len(rows) > maximum
            or any(type(row) is not tuple for row in rows)):
        _fail('invalid_source')
    return rows


def _native_commit(conn: sqlite3.Connection) -> None:
    """Narrow fault seam: the original body always calls the real base C method."""
    sqlite3.Connection.commit(conn)


def _native_close(conn: sqlite3.Connection) -> None:
    """Narrow fault seam; callers mark the attempt before this real C call."""
    sqlite3.Connection.close(conn)


def _code(primary: Exception) -> str:
    if type(primary) in (_HostFault, _OwnerFault, CurrentOwnerObservationError):
        value = primary.code
        return value if type(value) is str and value in _CODES else 'unavailable'
    return _error_code(primary)


def request_launch_stop_owned(
    *, business_store: BusinessStore, account_store: AccountStore, token: str,
    source: LaunchStopSourceHeaders, reason: str,
) -> LaunchStopIntentFact:
    """Return only after one real commit, close and post-identity confirmation.

    No caller-supplied connection/path/time/callback/bool or runtime cleanup.
    An ordinary failure once commit starts is unknown; no write is retried.
    BaseException is preserved, even if rollback/close subsequently also fail.
    A fact is a durable Stop request, never an execution or budget permission.
    """
    conn = None
    primary = None
    result = None
    commit_started = False
    close_started = False
    try:
        captured = _capture(business_store, account_store, token, source, reason)
        budget = _ReadBudget()
        before = _file_snapshot(captured.path)
        _safe_sidecars(captured.path)
        versions = _database_header(captured.path, before)
        _source_unchanged(captured)
        _safe_sidecars(captured.path)
        conn = sqlite3.connect(captured.path.as_uri() + '?mode=rw', uri=True,
                               timeout=0.0, isolation_level=None)
        if type(conn) is not sqlite3.Connection:
            _fail('invalid_source')
        # No SELECT/PRAGMA occurs on this connection before the owned write lock.
        _sql_rows(conn, 'BEGIN IMMEDIATE', 0)
        if (conn.in_transaction is not True or conn.row_factory is not None
                or conn.text_factory is not str):
            _fail('invalid_source')
        if _sql_rows(conn, 'PRAGMA busy_timeout') != [(0,)]:
            _fail('invalid_source')
        if _sql_rows(conn, 'PRAGMA query_only') != [(0,)]:
            _fail('invalid_source')
        expected_mode = 'delete' if versions == (1, 1) else 'wal'
        if _sql_rows(conn, 'PRAGMA journal_mode') != [(expected_mode,)]:
            _fail('unavailable')
        _source_unchanged(captured)
        if _file_snapshot(captured.path) != before:
            _fail('invalid_source')
        _safe_sidecars(captured.path)
        if _database_header(captured.path, before) != versions:
            _fail('invalid_source')
        owner = _observe_current_owner_on_rw_connection(
            conn, account_store=account_store, business_store=business_store, token=token,
        )
        _source_unchanged(captured)
        # The original writer validates this server-generated naive-local time.
        requested_at = datetime.now().isoformat()
        result = request_launch_stop_on_connection(
            conn, business_store=business_store, account_store=account_store,
            token=token, source=source, reason=reason, requested_at=requested_at,
            _budget=budget,
        )
        if type(result) is not LaunchStopIntentFact:
            _fail('invalid_source')
        _source_unchanged(captured)
        final_owner = _observe_current_owner_on_rw_connection(
            conn, account_store=account_store, business_store=business_store, token=token,
        )
        if final_owner.login != owner.login:
            _fail('unavailable')
        _source_unchanged(captured)
        if (_file_snapshot(captured.path) != before
                or _database_header(captured.path, before) != versions):
            _fail('invalid_source')
        # A rollback journal is normal while this native write transaction is open.
        # It is not accepted before connect or after the completed close.
        _safe_sidecars(captured.path, _allow_owned_journal=True)
        _source_unchanged(captured)
        commit_started = True
        if _native_commit(conn) is not None or conn.in_transaction is not False:
            _fail('outcome_unknown')
        close_started = True
        if _native_close(conn) is not None:
            _fail('outcome_unknown')
        _source_unchanged(captured)
        if _file_snapshot(captured.path) != before:
            _fail('invalid_source')
        _safe_sidecars(captured.path)
        if _database_header(captured.path, before) != versions:
            _fail('invalid_source')
        _source_unchanged(captured)
    except BaseException as exc:
        primary = exc
    finally:
        if type(conn) is sqlite3.Connection and not close_started:
            # Only a failing path reaches cleanup with an unclosed connection.
            # Neither cleanup result may replace an already captured primary.
            try:
                sqlite3.Connection.rollback(conn)
            except BaseException as exc:
                if primary is None:
                    primary = exc
            close_started = True
            try:
                if _native_close(conn) is not None:
                    _fail('outcome_unknown')
            except BaseException as exc:
                if primary is None:
                    primary = exc
    # Outside all handlers: ordinary errors expose no original cause/context.
    if primary is not None:
        if not issubclass(type(primary), Exception):
            raise primary
        failure = 'outcome_unknown' if commit_started else _code(primary)
        raise LaunchStopIntentError(failure) from None
    return result
