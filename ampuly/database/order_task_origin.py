"""Private opt-in Task creation observations; no migration or authority grant.

Reference schema is never applied by this module. Only the integrated strict
Store insertion can establish first creation; this helper alone cannot prove
that a caller read an original claim, or authenticate a caller-made origin.
Facts retain no launch bytes, inputs, personnel or general Task metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from .source_project_scope import validate_source_project, require_source_project, SourceProjectScopeError


TABLE_NAME = "order_task_creation_facts_v1"
SOURCE_FIELDS = (
    "project_id", "order_id", "outbox_id", "workflow_id", "workflow_version",
    "launch_payload_sha256", "claim_event_id", "event_chain_head_sha256",
)
FACT_COLUMNS = ("fact_id", "task_id") + SOURCE_FIELDS
FINGERPRINT_VERSION = "order_task_creation.v1"
_MAX_ID_BYTES = 256
_MAX_SCHEMA_BYTES = 32768
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MESSAGES = {
    "invalid_source": "原订单 Task 创建来源无法核验。",
    "unavailable": "原订单 Task 创建来源暂不可用。",
    "conflict": "原订单 Task 创建事实已存在。",
}


class OrderTaskOriginError(ValueError):
    """Fixed public code/message without private SQL, values or diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _MESSAGES else "invalid_source"
        super().__init__(_MESSAGES[self.code])


@dataclass(frozen=True, repr=False)
class OrderTaskOrigin:
    """Server-only bounded observation, not permission or an unforgeable token."""

    project_id: str
    order_id: str
    outbox_id: str
    workflow_id: str
    workflow_version: str
    launch_payload_sha256: str
    claim_event_id: str
    event_chain_head_sha256: str


@dataclass(frozen=True, repr=False)
class OrderTaskCreationFact:
    """Private immutable fact; not current Task existence or read permission."""

    fact_id: str
    task_id: str
    origin: OrderTaskOrigin


# These fixed SQLite expressions implement shape checks, not input coercion.
# NUL must be handled separately because SQLite GLOB terminates at NUL.
_CONTROL_GLOB_SQL = (
    "('*[' || char(" + ",".join(str(number) for number in (
        *range(1, 32), *range(127, 160),
    )) + ") || ']*')"
)
_EDGE_WHITESPACE_SQL = "char(32,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288)"


def _identity_column(name: str, *, empty: bool = False) -> str:
    minimum = 0 if empty else 1
    return (
        f"{name} TEXT NOT NULL CHECK (typeof({name}) = 'text' "
        f"AND length(CAST({name} AS BLOB)) BETWEEN {minimum} AND {_MAX_ID_BYTES} "
        f"AND instr({name}, char(0)) = 0 AND {name} NOT GLOB {_CONTROL_GLOB_SQL} "
        f"AND trim({name}, {_EDGE_WHITESPACE_SQL}) = {name})"
    )


def _digest_column(name: str) -> str:
    return (
        f"{name} TEXT NOT NULL CHECK (typeof({name}) = 'text' "
        f"AND length(CAST({name} AS BLOB)) = 64 AND instr({name}, char(0)) = 0 "
        f"AND {name} NOT GLOB '*[^0-9a-f]*')"
    )


_COLUMN_DEFINITIONS = (
    _digest_column("fact_id"), _identity_column("task_id"),
    _identity_column("project_id"), _identity_column("order_id"),
    _identity_column("outbox_id"), _identity_column("workflow_id"),
    _identity_column("workflow_version", empty=True),
    _digest_column("launch_payload_sha256"), _identity_column("claim_event_id"),
    _digest_column("event_chain_head_sha256"),
)
_SCHEMA_OBJECTS = (
    ("table", TABLE_NAME),
    ("trigger", TABLE_NAME + "_no_update"),
    ("trigger", TABLE_NAME + "_no_delete"),
    ("trigger", TABLE_NAME + "_no_duplicate"),
)
SCHEMA_STATEMENTS = (
    "CREATE TABLE " + TABLE_NAME + " (\n    "
    + ",\n    ".join(_COLUMN_DEFINITIONS)
    + ",\n    PRIMARY KEY (fact_id),\n    UNIQUE (task_id)\n)",
    f"CREATE TRIGGER {TABLE_NAME}_no_update BEFORE UPDATE ON {TABLE_NAME}\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Task creation facts are append-only');\nEND",
    f"CREATE TRIGGER {TABLE_NAME}_no_delete BEFORE DELETE ON {TABLE_NAME}\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Task creation facts are append-only');\nEND",
    f"CREATE TRIGGER {TABLE_NAME}_no_duplicate BEFORE INSERT ON {TABLE_NAME}\n"
    f"WHEN EXISTS (SELECT 1 FROM {TABLE_NAME} "
    "WHERE fact_id = NEW.fact_id OR task_id = NEW.task_id)\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Task creation fact conflict');\nEND",
)


def _text(value: object, *, empty: bool = False) -> str:
    if (type(value) is not str or len(value) > _MAX_ID_BYTES
            or (not value and not empty) or value != value.strip()
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        raise OrderTaskOriginError("invalid_source")
    size = None
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        pass
    if size is None or size > _MAX_ID_BYTES:
        raise OrderTaskOriginError("invalid_source")
    return value


def _digest(value: object) -> str:
    if type(value) is not str or len(value) != 64 or _SHA256.fullmatch(value) is None:
        raise OrderTaskOriginError("invalid_source")
    return value


def _origin_values(origin: OrderTaskOrigin, *, project_id: str) -> tuple[str, ...]:
    project_id = _project_id(project_id)
    if type(origin) is not OrderTaskOrigin:
        raise OrderTaskOriginError("invalid_source")
    values = tuple(getattr(origin, field) for field in SOURCE_FIELDS)
    for field, value in zip(SOURCE_FIELDS, values):
        if field in ("launch_payload_sha256", "event_chain_head_sha256"):
            _digest(value)
        else:
            _text(value, empty=field == "workflow_version")
    if values[0] != project_id:
        raise OrderTaskOriginError("invalid_source")
    return values


def _fingerprint(values: tuple[str, ...], task_id: str) -> str:
    raw = json.dumps(
        [FINGERPRINT_VERSION, *values, task_id], ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_order_task_origin(origin: OrderTaskOrigin, *, project_id: str) -> None:
    """Validate the exact private schema/scope before any Store effects."""
    failed = False
    try:
        _origin_values(origin, project_id=project_id)
    except Exception:
        failed = True
    if failed:
        raise OrderTaskOriginError("invalid_source") from None


def _rows(conn: sqlite3.Connection, statement: str, parameters: tuple) -> list[tuple]:
    cursor = conn.execute(statement, parameters)
    primary_failure = False
    try:
        # Fixed row queries carry LIMIT 2; query_only is a bounded PRAGMA.
        # Also cap fetching if a provider violates that fixed SQL contract.
        rows = cursor.fetchmany(3)
        if len(rows) > 2 or any(type(row) is not tuple for row in rows):
            raise OrderTaskOriginError("invalid_source")
        return rows
    except BaseException:
        primary_failure = True
        raise
    finally:
        try:
            cursor.close()
        except BaseException:
            if not primary_failure:
                raise


def _connection(conn: sqlite3.Connection, *, write: bool) -> None:
    if type(conn) is not sqlite3.Connection:
        raise OrderTaskOriginError("invalid_source")
    if (not conn.in_transaction or conn.row_factory is not None or conn.text_factory is not str):
        raise OrderTaskOriginError("invalid_source")
    # The integrated Store owns BEGIN IMMEDIATE. This helper neither begins a
    # transaction nor treats in_transaction as proof of first Task creation.
    if write:
        rows = _rows(conn, "PRAGMA query_only", ())
        if len(rows) != 1 or rows[0] != (0,):
            raise OrderTaskOriginError("invalid_source")


def _project_id(value: object) -> str:
    try:
        return validate_source_project(value)
    except SourceProjectScopeError:
        raise OrderTaskOriginError("invalid_source") from None


def _project_connection(conn: sqlite3.Connection, project_id: str, *, write: bool = False) -> None:
    try:
        require_source_project(conn, project_id, write=write)
    except SourceProjectScopeError as error:
        raise OrderTaskOriginError(error.code) from None


def _normalized_schema(statement: str) -> str:
    normalized = statement.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    return normalized


def _decode(raw: bytes) -> str:
    result = None
    try:
        result = raw.decode("utf-8")
    except UnicodeError:
        pass
    if result is None:
        raise OrderTaskOriginError("invalid_source") from None
    return result


def _schema(conn: sqlite3.Connection) -> None:
    for (kind, name), expected in zip(_SCHEMA_OBJECTS, SCHEMA_STATEMENTS):
        metadata = _rows(
            conn, "SELECT typeof(sql), length(CAST(sql AS BLOB)) FROM sqlite_master "
            "WHERE type = ? AND name = ? LIMIT 2", (kind, name),
        )
        if not metadata:
            raise OrderTaskOriginError("unavailable" if kind == "table" else "invalid_source")
        if (len(metadata) != 1 or len(metadata[0]) != 2 or metadata[0][0] != "text"
                or type(metadata[0][1]) is not int or not 0 < metadata[0][1] <= _MAX_SCHEMA_BYTES):
            raise OrderTaskOriginError("invalid_source")
        rows = _rows(
            conn, "SELECT substr(CAST(sql AS BLOB), 1, ?) FROM sqlite_master "
            "WHERE type = ? AND name = ? LIMIT 2", (_MAX_SCHEMA_BYTES + 1, kind, name),
        )
        if (len(rows) != 1 or len(rows[0]) != 1 or type(rows[0][0]) is not bytes
                or len(rows[0][0]) != metadata[0][1]):
            raise OrderTaskOriginError("invalid_source")
        actual = _decode(rows[0][0])
        if _normalized_schema(actual) != _normalized_schema(expected):
            raise OrderTaskOriginError("invalid_source")


def _bounded_record(
    conn: sqlite3.Connection, *, table: str, columns: tuple[str, ...],
    task_id: str,
) -> tuple[str, ...] | None:
    # Callers pass only fixed private constants, never caller-selected SQL.
    metadata_columns = ", ".join(
        f"typeof({column}), length(CAST({column} AS BLOB))" for column in columns
    )
    metadata = _rows(
        conn, f"SELECT {metadata_columns} FROM {table} WHERE "
        + ("id" if table == "tasks" else "task_id") + " = ? LIMIT 2", (task_id,),
    )
    if not metadata:
        return None
    if len(metadata) != 1 or len(metadata[0]) != len(columns) * 2:
        raise OrderTaskOriginError("invalid_source")
    sizes = []
    for offset in range(0, len(metadata[0]), 2):
        kind, size = metadata[0][offset:offset + 2]
        if kind != "text" or type(size) is not int or not 0 <= size <= _MAX_ID_BYTES:
            raise OrderTaskOriginError("invalid_source")
        sizes.append(size)
    # SQLite substr of an empty BLOB is NULL. Preserve its existing allowed
    # empty text without treating a non-text/NULL value as valid input.
    selections = ", ".join(
        f"CASE WHEN length(CAST({column} AS BLOB)) = 0 THEN CAST({column} AS BLOB) "
        f"ELSE substr(CAST({column} AS BLOB), 1, {_MAX_ID_BYTES + 1}) END"
        for column in columns
    )
    rows = _rows(
        conn, f"SELECT {selections} FROM {table} WHERE "
        + ("id" if table == "tasks" else "task_id") + " = ? LIMIT 2", (task_id,),
    )
    if len(rows) != 1 or len(rows[0]) != len(columns):
        raise OrderTaskOriginError("invalid_source")
    result = []
    for raw, size in zip(rows[0], sizes):
        if type(raw) is not bytes or len(raw) != size:
            raise OrderTaskOriginError("invalid_source")
        result.append(_decode(raw))
    return tuple(result)


def _append(
    conn: sqlite3.Connection, *, origin: OrderTaskOrigin, task_id: str,
) -> OrderTaskCreationFact:
    if type(origin) is not OrderTaskOrigin:
        raise OrderTaskOriginError("invalid_source")
    values = _origin_values(origin, project_id=origin.project_id)
    task_id = _text(task_id)
    _connection(conn, write=True)
    _project_connection(conn, values[0], write=True)
    _schema(conn)
    task = _bounded_record(conn, table="tasks", columns=("id", "project_id"), task_id=task_id)
    if task is None or task != (task_id, values[0]):
        raise OrderTaskOriginError("invalid_source")
    fact_id = _fingerprint(values, task_id)
    duplicate = _rows(
        conn, f"SELECT 1 FROM {TABLE_NAME} WHERE fact_id = ? OR task_id = ? LIMIT 2",
        (fact_id, task_id),
    )
    if duplicate:
        raise OrderTaskOriginError("conflict")
    cursor = conn.execute(
        f"INSERT INTO {TABLE_NAME} ({', '.join(FACT_COLUMNS)}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (fact_id, task_id, *values),
    )
    try:
        if cursor.rowcount != 1:
            raise OrderTaskOriginError("unavailable")
    finally:
        cursor.close()
    return OrderTaskCreationFact(fact_id, task_id, OrderTaskOrigin(*values))


def append_order_task_creation(
    conn: sqlite3.Connection, *, origin: OrderTaskOrigin, task_id: str,
) -> OrderTaskCreationFact:
    """Append inside the caller's write transaction; never own its lifecycle.

    Only a strict Store INSERT followed by this append on the SAME connection
    proves integrated creation. Standalone SELECT + append proves no such thing.
    No commit, rollback, close, reopening, DDL, retry or repair occurs here.
    """
    failure = None
    result = None
    try:
        result = _append(conn, origin=origin, task_id=task_id)
    except OrderTaskOriginError as exc:
        failure = exc.code if type(exc) is OrderTaskOriginError else "unavailable"
    except Exception:
        failure = "unavailable"
    if failure is not None:
        raise OrderTaskOriginError(failure) from None
    return result


def _read(
    conn: sqlite3.Connection, *, project_id: str, task_id: str,
) -> OrderTaskCreationFact | None:
    project_id = _text(project_id)
    task_id = _text(task_id)
    _connection(conn, write=False)
    _project_connection(conn, project_id)
    _schema(conn)
    row = _bounded_record(conn, table=TABLE_NAME, columns=FACT_COLUMNS, task_id=task_id)
    if row is None:
        return None
    fact_id, stored_task_id = _digest(row[0]), _text(row[1])
    origin = OrderTaskOrigin(*row[2:])
    values = _origin_values(origin, project_id=project_id)
    if stored_task_id != task_id or fact_id != _fingerprint(values, task_id):
        raise OrderTaskOriginError("invalid_source")
    return OrderTaskCreationFact(fact_id, task_id, origin)


def read_order_task_creation(
    conn: sqlite3.Connection, *, project_id: str, task_id: str,
) -> OrderTaskCreationFact | None:
    """Read one indexed private fact in an existing explicit transaction.

    Missing fact is None, missing schema is unavailable. No Task metadata/entity
    read, scan, backfill, transaction control or authorization grant occurs.
    Facts may remain after Task deletion; scope is not read permission.
    """
    failure = None
    result = None
    try:
        result = _read(conn, project_id=project_id, task_id=task_id)
    except OrderTaskOriginError as exc:
        failure = exc.code if type(exc) is OrderTaskOriginError else "unavailable"
    except Exception:
        failure = "unavailable"
    if failure is not None:
        raise OrderTaskOriginError(failure) from None
    return result
