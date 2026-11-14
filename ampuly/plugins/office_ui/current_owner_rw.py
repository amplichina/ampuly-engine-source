"""Observe the current owner on the host's existing owned RW transaction.

This is not a Grant, authentication receipt, Stop barrier or reusable permission.
The host owns locking and BEGIN IMMEDIATE; in_transaction cannot prove that mode.
No connection/transaction/token mutation occurs. V6 salted-hash fingerprints
are read only to revalidate the durable session on the same transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import time

from .accounts import AccountStore, ROLES
from .business_store import BusinessStore
from .current_account_principal import (
    _MAX_ID_BYTES, _PRINCIPAL_SQL, _PrincipalFault, _account_schema,
    _customers, _login, _rows, _text, CurrentAccountPrincipalError,
)
from . import console_session_store as _sessions


_FIXED_ERRORS = {
    "invalid_source": "当前账号记录或事务来源不符合要求。",
    "unavailable": "当前账号状态暂时无法确认。",
    "forbidden": "当前会话没有有效的所有者身份。",
}
_MISSING = object()


class CurrentOwnerObservationError(ValueError):
    """Three fixed codes; no token, source path, account row or SQL detail."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in _FIXED_ERRORS:
            raise ValueError("当前所有者观察错误代码不符合要求。")
        self.code = code
        super().__init__(_FIXED_ERRORS[code])


class _OwnerFault(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class CurrentOwnerObservation:
    """Only observed login; freely constructible and never action authority."""

    login: str


def _fail(code: str) -> None:
    raise _OwnerFault(code)


def _token(value: object) -> str:
    if (type(value) is not str or not value or len(value) > 512
            or value.strip() != value
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        _fail("invalid_source")
    invalid_utf8 = False
    try:
        invalid_utf8 = len(value.encode("utf-8")) > 512
    except UnicodeError:
        invalid_utf8 = True
    if invalid_utf8:
        _fail("invalid_source")
    return value


def _now() -> int | float:
    value = time.time()
    if (type(value) not in (int, float) or value < 0
            or type(value) is int and value > 2**63 - 1
            or type(value) is float and not math.isfinite(value)):
        _fail("unavailable")
    return value


def _source_path(value: object, cwd: Path) -> Path:
    if (type(value) is not type(cwd) or value.drive.startswith("\\\\")
            or value.drive and not value.root):
        _fail("invalid_source")
    result = type(cwd)(os.path.normpath(os.fspath(value if value.is_absolute() else cwd / value)))
    _text(os.fspath(result), 4096)
    return result


def _owned_rw_connection(conn: object, *, expected_main: Path) -> None:
    if type(conn) is not sqlite3.Connection:
        _fail("invalid_source")
    if (conn.row_factory is not None and conn.row_factory is not sqlite3.Row
            or conn.text_factory is not str or conn.in_transaction is not True):
        _fail("invalid_source")
    query_only = _rows(conn, "PRAGMA query_only", (), 1)
    if query_only != [(0,)] or type(query_only[0][0]) is not int:
        _fail("invalid_source")
    databases = _rows(conn, "PRAGMA database_list", (), 2)
    main = None
    names = set()
    for row in databases:
        if (len(row) != 3 or type(row[0]) is not int or type(row[1]) is not str
                or row[1] not in {"main", "temp"} or row[1] in names
                or type(row[2]) is not str):
            _fail("invalid_source")
        names.add(row[1])
        if row[1] == "main":
            main = row[2]
    if main is None:
        _fail("invalid_source")
    _text(main, 4096)
    main_path = type(expected_main)(main)
    if (not main_path.is_absolute() or main_path.drive.startswith("\\\\")
            or main_path.drive and not main_path.root
            or type(expected_main)(os.path.normpath(main)) != expected_main):
        _fail("invalid_source")
    if _rows(conn, "SELECT 1 FROM temp.sqlite_master LIMIT 1", (), 1):
        _fail("invalid_source")


def _observe_current_owner_on_rw_connection(
    conn: sqlite3.Connection,
    *,
    account_store: AccountStore,
    business_store: BusinessStore,
    token: str,
) -> CurrentOwnerObservation:
    """Observe a live original token and native current-owner row, same conn.

    Reuses B60 validators/schema/SQL; only the owner subset predicate is here,
    not the general Account decoder. Missing/expired/disabled/nonowner is never
    local bootstrap. No token is deleted or refreshed. The host must observe
    again after its write and before commit, under its real locks, not trust a
    retained DTO. This function neither starts nor finishes a transaction.
    """

    failure = None
    result = None
    try:
        if type(account_store) is not AccountStore or type(business_store) is not BusinessStore:
            _fail("invalid_source")
        account_fields = account_store.__dict__
        business_fields = business_store.__dict__
        if (type(account_fields) is not dict or type(business_fields) is not dict
                or account_fields.get("business") is not business_store):
            _fail("invalid_source")
        token = _token(token)
        tokens = account_fields.get("_tokens")
        if type(tokens) is not dict:
            _fail("invalid_source")
        entry = tokens.get(token, _MISSING)
        if entry is _MISSING:
            _fail("forbidden")
        if type(entry) is not tuple or len(entry) != 2:
            _fail("invalid_source")
        login_key = _login(entry[0])
        expires = entry[1]
        if (login_key.lower() != login_key or type(expires) not in (int, float) or expires <= 0
                or type(expires) is int and expires > 2**63 - 1
                or type(expires) is float and not math.isfinite(expires)):
            _fail("invalid_source")
        cwd = Path.cwd().absolute()
        raw_home = account_fields.get("home")
        raw_path = business_fields.get("path")
        home = _source_path(raw_home, cwd)
        path = _source_path(raw_path, cwd)
        if path.name != "business.db" or path != home / "business.db":
            _fail("invalid_source")

        def source_unchanged() -> None:
            if (type(account_store) is not AccountStore or type(business_store) is not BusinessStore
                    or account_store.__dict__ is not account_fields
                    or business_store.__dict__ is not business_fields
                    or account_fields.get("business") is not business_store
                    or account_fields.get("home") is not raw_home
                    or business_fields.get("path") is not raw_path
                    or Path.cwd().absolute() != cwd
                    or _source_path(raw_home, cwd) != home or _source_path(raw_path, cwd) != path):
                _fail("invalid_source")
            if account_fields.get("_tokens") is not tokens or tokens.get(token, _MISSING) is not entry:
                _fail("unavailable")
            if _now() > expires:
                _fail("forbidden")

        source_unchanged()
        _owned_rw_connection(conn, expected_main=path)
        # A cached tuple is not authority after durable revocation/password reset.
        # Reuse the bounded C1 reader on this owned RW connection: no second
        # connection, DDL, transaction, cache mutation, or legacy fallback.
        with _sessions.boundary():
            _sessions.require_migrated(conn)
            workspace = _sessions.workspace_identity(path, conn)
            session = _sessions.read(conn, hashlib.sha256(token.encode('utf-8')).hexdigest(),
                                     now=_now(), workspace_binding=workspace)
        if (session is None or session['login_key'] != login_key
                or session['expires_at'] != expires):
            _fail('forbidden')
        _account_schema(conn)
        rows = _rows(conn, _PRINCIPAL_SQL, (login_key,), 2)
        if len(rows) > 1:
            _fail("invalid_source")
        if not rows:
            _fail("forbidden")
        row = rows[0]
        if len(row) != 6:
            _fail("invalid_source")
        stored_login = _login(row[0])
        stored_key = _login(row[1])
        role = _text(row[2], 16)
        _text(row[3], _MAX_ID_BYTES, allow_empty=True)
        disabled = row[4]
        customers = _customers(row[5])
        if (stored_key != login_key or stored_login.lower() != login_key
                or stored_key != stored_key.lower() or role not in ROLES
                or type(disabled) is not int or disabled not in (0, 1)):
            _fail("invalid_source")
        if role == "owner" and customers:
            _fail("invalid_source")
        if role != "owner" or disabled != 0:
            _fail("forbidden")
        _owned_rw_connection(conn, expected_main=path)
        source_unchanged()
        result = CurrentOwnerObservation(login=stored_login)
    except (_OwnerFault, _PrincipalFault, CurrentAccountPrincipalError) as exc:
        failure = exc.code if type(exc.code) is str and exc.code in _FIXED_ERRORS else "unavailable"
    except Exception:
        failure = "unavailable"
    if failure is not None:
        raise CurrentOwnerObservationError(failure) from None
    return result
