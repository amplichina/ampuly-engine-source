"""Bounded console-session rows, called only on the owning business connection.

No bearer token is accepted here: only its SHA256. The caller owns transactions
and database-file identity checks; reads never initialize or repair a database.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from .current_account_principal import CurrentAccountPrincipalError

TTL_SECONDS = 12 * 3600
_MAX_TIME = 253402300799
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_LOGIN = re.compile(r"[a-z0-9_.@\-]{2,64}\Z")
_COLUMNS = (
    (0, "token_sha256", "TEXT", 0, None, 1, 0),
    (1, "login_key", "TEXT", 1, None, 0, 0),
    (2, "issued_at", "REAL", 1, None, 0, 0),
    (3, "expires_at", "REAL", 1, None, 0, 0),
    (4, "revoked_at", "REAL", 0, None, 0, 0),
    (5, "schema_version", "INTEGER", 1, None, 0, 0),
    (6, "credential_fingerprint", "TEXT", 1, None, 0, 0),
    (7, "workspace_binding", "TEXT", 1, None, 0, 0),
)


def _invalid():
    raise CurrentAccountPrincipalError("invalid_source") from None


@contextmanager
def boundary():
    """Do not expose SQL, credentials, paths or implementation exceptions."""
    try:
        yield
    except Exception as exc:
        code = "unavailable"
        if (type(exc) is CurrentAccountPrincipalError
                and type(exc.code) is str
                and exc.code in {"invalid_source", "unavailable"}):
            code = exc.code
        raise CurrentAccountPrincipalError(code) from None


def digest(value):
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        _invalid()
    return value


def login(value):
    if type(value) is not str or _LOGIN.fullmatch(value) is None:
        _invalid()
    return value


def moment(value):
    if (type(value) not in (int, float) or not 0 < value <= _MAX_TIME
            or not math.isfinite(value)):
        _invalid()
    return value


def times(issued_at, expires_at):
    moment(issued_at)
    moment(expires_at)
    if not 0 < expires_at - issued_at <= TTL_SECONDS:
        _invalid()


def fingerprint(salt, password_hash):
    if (type(salt) is not str or _HEX32.fullmatch(salt) is None
            or type(password_hash) is not str or _HEX64.fullmatch(password_hash) is None):
        _invalid()
    return hashlib.sha256((salt + ":" + password_hash).encode("ascii")).hexdigest()


def _rows(conn, sql, parameters=(), *, maximum=2):
    if type(conn) is not sqlite3.Connection:
        _invalid()
    cursor = sqlite3.Connection.execute(conn, sql, parameters)
    try:
        rows = sqlite3.Cursor.fetchmany(cursor, maximum + 1)
        if len(rows) > maximum:
            _invalid()
        return [tuple(row) for row in rows]
    finally:
        sqlite3.Cursor.close(cursor)


def _workspace_state(path):
    """Hash this exact main file's path/dev/ino; never persist its raw path.

    Stable across an ordinary process restart, different for a copied or
    replaced file. This does NOT prevent an in-place backup rollback which
    preserves the inode; anti-rollback requires an independent later contract.
    """
    cwd = Path.cwd().absolute()
    if (type(path) is not type(cwd) or path.drive.startswith("\\\\")
            or path.drive and not path.root):
        _invalid()
    normalized = type(cwd)(os.path.normpath(os.fspath(path if path.is_absolute() else cwd / path)))
    if normalized.name != "business.db" or not 1 <= len(normalized.parts) <= 16:
        _invalid()
    cursor = type(cwd)(normalized.parts[0])
    for index, part in enumerate(normalized.parts):
        if index:
            cursor = cursor / part
        info = cursor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _invalid()
        if index < len(normalized.parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                _invalid()
        elif (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
              or type(info.st_dev) is not int or info.st_dev < 0
              or type(info.st_ino) is not int or info.st_ino <= 0):
            _invalid()
    again = normalized.lstat()
    if (again.st_dev, again.st_ino, again.st_nlink) != (info.st_dev, info.st_ino, info.st_nlink):
        _invalid()
    value = {"schema": "ampuly.console.workspace.v1",
             "path": os.path.normcase(str(normalized)),
             "dev": info.st_dev, "ino": info.st_ino}
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return normalized, digest


def workspace_path_identity(path):
    """Capture before opening a fresh writer; missing files are not created."""
    return _workspace_state(path)[1]


def workspace_identity(path, conn):
    normalized, before = _workspace_state(path)
    # Only this freshly owned connection is inspected, never the original
    # writer. Principal observation may initialize its empty in-memory TEMP.
    databases = _rows(conn, "PRAGMA database_list", maximum=2)
    if len(databases) not in (1, 2):
        _invalid()
    main = databases[0]
    if (len(main) != 3 or type(main[0]) is not int or main[:2] != (0, "main")
            or type(main[2]) is not str
            or os.path.normcase(os.path.normpath(main[2])) != os.path.normcase(str(normalized))):
        _invalid()
    if len(databases) == 2:
        temporary = databases[1]
        if (len(temporary) != 3 or type(temporary[0]) is not int
                or temporary != (1, "temp", "")
                or _rows(conn, "SELECT 1 FROM temp.sqlite_master LIMIT 1", maximum=1)):
            _invalid()
    if _workspace_state(path)[1] != before:
        _invalid()
    return before


def schema(conn):
    if _rows(conn, "SELECT type,tbl_name FROM main.sqlite_schema "
             "WHERE name='console_sessions'", maximum=1) != [("table", "console_sessions")]:
        _invalid()
    if tuple(_rows(conn, "PRAGMA main.table_xinfo('console_sessions')", maximum=8)) != _COLUMNS:
        _invalid()
    indexes = _rows(conn, "PRAGMA main.index_list('console_sessions')", maximum=2)
    expected = {("sqlite_autoindex_console_sessions_1", 1, "pk", 0),
                ("console_sessions_login_key", 0, "c", 0)}
    if len(indexes) != 2 or {row[1:] for row in indexes} != expected:
        _invalid()
    if _rows(conn, "PRAGMA main.index_info('sqlite_autoindex_console_sessions_1')", maximum=1) != [(0, 0, "token_sha256")]:
        _invalid()
    if _rows(conn, "PRAGMA main.index_info('console_sessions_login_key')", maximum=1) != [(0, 1, "login_key")]:
        _invalid()
    if _rows(conn, "SELECT name FROM main.sqlite_schema WHERE type='trigger' "
             "AND tbl_name='console_sessions' LIMIT 1", maximum=1):
        _invalid()  # No session write may trigger another business mutation.


def require_migrated(conn):
    """Authority entry points require this explicitly supported migration."""
    rows = _rows(conn, "PRAGMA main.user_version", maximum=1)
    if rows != [(6,)] or type(rows[0][0]) is not int:
        raise CurrentAccountPrincipalError("unavailable") from None


def issue(conn, token_sha256, login_key, issued_at, expires_at,
          *, expected_salt, expected_password_hash, workspace_binding):
    digest(token_sha256)
    login(login_key)
    times(issued_at, expires_at)
    credential = fingerprint(expected_salt, expected_password_hash)
    digest(workspace_binding)
    schema(conn)
    rows = _rows(conn, "SELECT login_key,salt,password_hash,disabled FROM main.accounts "
                 "WHERE login_key=? LIMIT 2", (login_key,))
    if not rows:
        return False
    if len(rows) != 1:
        _invalid()
    key, salt, password_hash, disabled = rows[0]
    login(key)
    fingerprint(salt, password_hash)
    if type(disabled) is not int or disabled not in (0, 1):
        _invalid()
    if (disabled or key != login_key
            or not secrets.compare_digest(salt, expected_salt)
            or not secrets.compare_digest(password_hash, expected_password_hash)):
        return False
    if _rows(conn, "SELECT 1 FROM main.console_sessions WHERE token_sha256=? LIMIT 1",
             (token_sha256,), maximum=1):
        _invalid()  # Never replace a revoked/expired row with a fresh grant.
    cursor = sqlite3.Connection.execute(conn,
        "INSERT INTO main.console_sessions(token_sha256,login_key,issued_at,expires_at,"
        "revoked_at,schema_version,credential_fingerprint,workspace_binding) VALUES(?,?,?,?,NULL,1,?,?)",
        (token_sha256, login_key, issued_at, expires_at, credential, workspace_binding))
    sqlite3.Cursor.close(cursor)
    return True


def read(conn, token_sha256, *, now, workspace_binding):
    digest(token_sha256)
    moment(now)
    digest(workspace_binding)
    schema(conn)
    rows = _rows(conn, "SELECT login_key,issued_at,expires_at,revoked_at,schema_version,"
                 "credential_fingerprint,workspace_binding FROM main.console_sessions WHERE token_sha256=? LIMIT 2",
                 (token_sha256,))
    if not rows:
        return None
    if len(rows) != 1:
        _invalid()
    key, issued, expires, revoked, version, credential, saved_workspace = rows[0]
    login(key)
    times(issued, expires)
    digest(credential)
    digest(saved_workspace)
    if type(version) is not int or version != 1:
        _invalid()
    if not secrets.compare_digest(saved_workspace, workspace_binding):
        return None
    if revoked is not None:
        moment(revoked)
        return None
    if now < issued or now > expires:
        return None
    accounts = _rows(conn, "SELECT login_key,salt,password_hash,disabled FROM main.accounts "
                     "WHERE login_key=? LIMIT 2", (key,))
    if not accounts:
        return None
    if len(accounts) != 1:
        _invalid()
    current_key, salt, password_hash, disabled = accounts[0]
    login(current_key)
    current_credential = fingerprint(salt, password_hash)
    if type(disabled) is not int or disabled not in (0, 1):
        _invalid()
    if disabled or current_key != key or not secrets.compare_digest(current_credential, credential):
        return None
    return {"login_key": key, "issued_at": issued, "expires_at": expires}


def revoke_token(conn, token_sha256, *, now):
    digest(token_sha256)
    moment(now)
    schema(conn)
    cursor = sqlite3.Connection.execute(conn,
        "UPDATE main.console_sessions SET revoked_at=? WHERE token_sha256=? AND revoked_at IS NULL",
        (now, token_sha256))
    sqlite3.Cursor.close(cursor)


def revoke_login(conn, login_key, *, now):
    login(login_key)
    moment(now)
    schema(conn)
    cursor = sqlite3.Connection.execute(conn,
        "UPDATE main.console_sessions SET revoked_at=? WHERE login_key=? AND revoked_at IS NULL",
        (now, login_key))
    sqlite3.Cursor.close(cursor)
