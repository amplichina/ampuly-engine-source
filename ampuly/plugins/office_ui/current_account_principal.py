"""A narrow current account observation in the caller's owned RO transaction.

No connection, initialization, transaction ownership, credential read, login,
session validation, policy grant, worker start or bootstrap fallback lives here.
The original Account result is mutable observation, never authentication.
"""
from __future__ import annotations

import sqlite3

from .accounts import Account, ROLES, _LOGIN_RE


_FIXED_ERRORS = {
    "invalid_source": "当前账号记录或读取条件不符合要求。",
    "unavailable": "当前账号记录暂时无法读取。",
}
_MAX_LOGIN_BYTES = 64
_MAX_ID_BYTES = 256
_MAX_CUSTOMERS = 256
_MAX_CUSTOMER_CSV_BYTES = _MAX_CUSTOMERS * _MAX_ID_BYTES + _MAX_CUSTOMERS - 1
_ACCOUNT_COLUMNS = (
    (0, "login", "TEXT", 0, None, 1, 0),
    (1, "login_key", "TEXT", 1, None, 0, 0),
    (2, "role", "TEXT", 1, None, 0, 0),
    (3, "employee_id", "TEXT", 1, "''", 0, 0),
    (4, "display_name", "TEXT", 1, "''", 0, 0),
    (5, "disabled", "INTEGER", 1, "0", 0, 0),
    (6, "salt", "TEXT", 1, None, 0, 0),
    (7, "password_hash", "TEXT", 1, None, 0, 0),
    (8, "created_at", "TEXT", 1, None, 0, 0),
    (9, "last_login_at", "TEXT", 1, "''", 0, 0),
    (10, "customer_ids", "TEXT", 1, "''", 0, 0),
)
_ACCOUNT_INDEXES = {
    "sqlite_autoindex_accounts_1": ("pk", 0, "login"),
    "sqlite_autoindex_accounts_2": ("u", 1, "login_key"),
}
_INDEX_XINFO_SQL = {
    "sqlite_autoindex_accounts_1": "PRAGMA main.index_xinfo('sqlite_autoindex_accounts_1')",
    "sqlite_autoindex_accounts_2": "PRAGMA main.index_xinfo('sqlite_autoindex_accounts_2')",
}
_PRINCIPAL_SQL = """SELECT
    CASE WHEN typeof(login) = 'text'
              AND length(CAST(login AS BLOB)) BETWEEN 2 AND 64
         THEN login ELSE NULL END,
    CASE WHEN typeof(login_key) = 'text'
              AND length(CAST(login_key AS BLOB)) BETWEEN 2 AND 64
         THEN login_key ELSE NULL END,
    CASE WHEN typeof(role) = 'text'
              AND length(CAST(role AS BLOB)) BETWEEN 1 AND 16
         THEN role ELSE NULL END,
    CASE WHEN typeof(employee_id) = 'text'
              AND length(CAST(employee_id AS BLOB)) <= 256
         THEN employee_id ELSE NULL END,
    CASE WHEN typeof(disabled) = 'integer' AND disabled IN (0, 1)
         THEN disabled ELSE NULL END,
    CASE WHEN typeof(customer_ids) = 'text'
              AND length(CAST(customer_ids AS BLOB)) <= 65791
         THEN customer_ids ELSE NULL END
FROM main.accounts INDEXED BY sqlite_autoindex_accounts_2
WHERE login_key = ? LIMIT 2"""


class CurrentAccountPrincipalError(ValueError):
    """Only two fixed codes and fixed text; never expose data or SQL errors."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in _FIXED_ERRORS:
            raise ValueError("当前账号读取错误代码不符合要求。")
        self.code = code
        super().__init__(_FIXED_ERRORS[code])


class _PrincipalFault(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _invalid() -> None:
    raise _PrincipalFault("invalid_source")


def _unavailable() -> None:
    raise _PrincipalFault("unavailable")


def _rows(conn: sqlite3.Connection, sql: str, parameters: tuple, maximum: int) -> list[tuple]:
    """Close a bounded native cursor without masking its primary exception."""

    cursor = None
    primary = None
    raw_rows = None
    try:
        cursor = sqlite3.Connection.execute(conn, sql, parameters)
        raw_rows = sqlite3.Cursor.fetchmany(cursor, maximum + 1)
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
    if type(raw_rows) is not list or len(raw_rows) > maximum:
        _invalid()
    result = []
    for row in raw_rows:
        if type(row) is sqlite3.Row:
            row = tuple(row)
        elif type(row) is not tuple:
            _invalid()
        result.append(row)
    return result


def _text(value: object, maximum: int, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        _invalid()
    if len(value) > maximum:
        _invalid()
    failed = False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        failed = True
        size = maximum + 1
    if (failed or size > maximum or not allow_empty and not value
            or value.strip() != value or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        _invalid()
    return value


def _login(value: object) -> str:
    value = _text(value, _MAX_LOGIN_BYTES)
    if _LOGIN_RE.fullmatch(value) is None:
        _invalid()
    return value


def _owned_read_connection(conn: object) -> None:
    if type(conn) is not sqlite3.Connection:
        _invalid()
    if (conn.row_factory is not None and conn.row_factory is not sqlite3.Row
            or conn.text_factory is not str or conn.in_transaction is not True):
        _invalid()
    query_only = _rows(conn, "PRAGMA query_only", (), 1)
    if query_only != [(1,)]:
        _invalid()
    databases = _rows(conn, "PRAGMA database_list", (), 2)
    if not databases:
        _invalid()
    names = set()
    for row in databases:
        if (len(row) != 3 or type(row[0]) is not int or type(row[1]) is not str
                or row[1] not in {"main", "temp"} or type(row[2]) is not str or row[1] in names):
            _invalid()
        names.add(row[1])
    if "main" not in names:
        _invalid()
    # A caller with any TEMP object is not the fresh, unshadowed transaction
    # this first private observation slice accepts, even though SQL is main-qualified.
    shadow = _rows(conn, "SELECT 1 FROM temp.sqlite_master LIMIT 1", (), 1)
    if shadow:
        _invalid()


def _account_schema(conn: sqlite3.Connection) -> None:
    table = _rows(conn, """SELECT type, name, tbl_name,
        CASE WHEN typeof(sql) = 'text' AND length(CAST(sql AS BLOB)) <= 8192
             THEN sql ELSE NULL END
        FROM main.sqlite_master WHERE name = 'accounts' LIMIT 2""", (), 2)
    if not table:
        _unavailable()
    if (len(table) != 1 or len(table[0]) != 4 or table[0][:3] != ("table", "accounts", "accounts")
            or type(table[0][3]) is not str):
        _invalid()
    columns = _rows(conn, "PRAGMA main.table_xinfo('accounts')", (), 32)
    if len(columns) != len(_ACCOUNT_COLUMNS):
        _invalid()
    for actual, expected in zip(columns, _ACCOUNT_COLUMNS):
        if (len(actual) != 7 or actual != expected
                or any(type(actual[index]) is not int for index in (0, 3, 5, 6))
                or any(type(actual[index]) is not str for index in (1, 2))):
            _invalid()
    schema_indexes = _rows(conn, """SELECT
        CASE WHEN typeof(name) = 'text' AND length(CAST(name AS BLOB)) <= 128
             THEN name ELSE NULL END, type, tbl_name,
        CASE WHEN sql IS NULL THEN 1 ELSE 0 END
        FROM main.sqlite_master WHERE type = 'index' AND tbl_name = 'accounts' LIMIT 17""", (), 16)
    if len(schema_indexes) != 2:
        _invalid()
    expected_names = set(_ACCOUNT_INDEXES)
    if {row[0] for row in schema_indexes if len(row) == 4 and type(row[0]) is str} != expected_names:
        _invalid()
    if any(len(row) != 4 or row[1:] != ("index", "accounts", 1)
           or type(row[3]) is not int for row in schema_indexes):
        _invalid()
    indexes = _rows(conn, "PRAGMA main.index_list('accounts')", (), 16)
    if len(indexes) != 2:
        _invalid()
    seen = set()
    for row in indexes:
        if (len(row) != 5 or type(row[0]) is not int or not 0 <= row[0] < 16
                or type(row[1]) is not str or row[1] not in _ACCOUNT_INDEXES or row[1] in seen
                or type(row[2]) is not int or row[2] != 1
                or type(row[3]) is not str or row[3] != _ACCOUNT_INDEXES[row[1]][0]
                or type(row[4]) is not int or row[4] != 0):
            _invalid()
        seen.add(row[1])
        info = _rows(conn, _INDEX_XINFO_SQL[row[1]], (), 8)
        _origin, cid, name = _ACCOUNT_INDEXES[row[1]]
        if info != [(0, cid, name, 0, "BINARY", 1), (1, -1, None, 0, "BINARY", 0)]:
            _invalid()
        if any(type(entry[index]) is not int for entry in info for index in (0, 1, 3, 5)):
            _invalid()
    if seen != expected_names:
        _invalid()


def _customers(value: object) -> tuple[str, ...]:
    value = _text(value, _MAX_CUSTOMER_CSV_BYTES, allow_empty=True)
    if not value:
        return ()
    parts = value.split(",", _MAX_CUSTOMERS)
    if len(parts) > _MAX_CUSTOMERS:
        _invalid()
    result = []
    seen = set()
    for part in parts:
        customer = _text(part, _MAX_ID_BYTES)
        if customer in seen:
            _invalid()
        seen.add(customer)
        result.append(customer)
    return tuple(result)


def _read_account_principal_on_connection(
    conn: sqlite3.Connection,
    *,
    login: str,
) -> Account | None:
    """Observe one current account, not authenticate or permit any action.

    The caller already owns this BEGIN/query_only transaction and keeps that
    responsibility afterwards. Missing means None, never local owner bootstrap.
    A disabled record is returned with disabled=True for the original resolver
    to refuse. Display/date defaults stay empty; no credential values are read.
    """

    failure = None
    result = None
    try:
        login_key = _login(login).lower()
        _owned_read_connection(conn)
        _account_schema(conn)
        rows = _rows(conn, _PRINCIPAL_SQL, (login_key,), 2)
        if len(rows) > 1:
            _invalid()
        if rows:
            row = rows[0]
            if len(row) != 6:
                _invalid()
            stored_login = _login(row[0])
            stored_key = _login(row[1])
            role = _text(row[2], 16)
            employee_id = _text(row[3], _MAX_ID_BYTES, allow_empty=True)
            disabled = row[4]
            customers = _customers(row[5])
            if (stored_key != login_key or stored_login.lower() != login_key
                    or stored_key != stored_key.lower() or role not in ROLES
                    or type(disabled) is not int or disabled not in (0, 1)):
                _invalid()
            if (role == "employee" and not employee_id
                    or role == "customer" and (employee_id or not customers)
                    or role == "owner" and customers):
                _invalid()
            result = Account(login=stored_login, role=role, employee_id=employee_id,
                             disabled=disabled == 1, customer_ids=customers)
        # Native SQL and construction are synchronous, but still leave ownership
        # exactly as supplied; no accepted query can silently finish a transaction.
        _owned_read_connection(conn)
    except _PrincipalFault as exc:
        failure = exc.code
    except Exception:
        failure = "unavailable"
    if failure is not None:
        raise CurrentAccountPrincipalError(failure) from None
    return result
