"""Private first-Run source observations, never a migration or authority grant.

Only the integrated strict Store transaction pairs first Run insertion with a
fact. A caller can fabricate the frozen observation; it is not a permission,
business attempt, EmployeeRunIdentity or independently trusted envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3

from . import order_task_origin as _task


TABLE_NAME = "order_run_creation_facts_v1"
FACT_COLUMNS = (
    "fact_id", "run_id", "project_id", "session_id", "source_task_id",
    "source_task_fact_id",
)
FINGERPRINT_VERSION = "order_run_creation.v1"
_MAX_ID_BYTES = _task._MAX_ID_BYTES
_MAX_SCHEMA_BYTES = _task._MAX_SCHEMA_BYTES
_MESSAGES = {
    "invalid_source": "原订单 Run 创建来源无法核验。",
    "unavailable": "原订单 Run 创建来源暂不可用。",
    "conflict": "原订单 Run 创建事实已存在。",
}


class OrderRunOriginError(ValueError):
    """Fixed public code/message without private values or SQL diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _MESSAGES else "invalid_source"
        super().__init__(_MESSAGES[self.code])


@dataclass(frozen=True, repr=False)
class PreparedOrderRunSource:
    """A bounded observation, not an unforgeable token or read permission."""

    run_id: str
    project_id: str
    session_id: str
    source_task_id: str
    source_task_fact_id: str


@dataclass(frozen=True, repr=False)
class OrderRunCreationFact:
    """Immutable original association, not current Task/Run existence."""

    fact_id: str
    run_id: str
    project_id: str
    session_id: str
    source_task_id: str
    source_task_fact_id: str


_COLUMN_DEFINITIONS = (
    _task._digest_column("fact_id"), _task._identity_column("run_id"),
    _task._identity_column("project_id"), _task._identity_column("session_id"),
    _task._identity_column("source_task_id"),
    _task._digest_column("source_task_fact_id"),
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
    + ",\n    PRIMARY KEY (fact_id),\n    UNIQUE (run_id)\n)",
    f"CREATE TRIGGER {TABLE_NAME}_no_update BEFORE UPDATE ON {TABLE_NAME}\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Run creation facts are append-only');\nEND",
    f"CREATE TRIGGER {TABLE_NAME}_no_delete BEFORE DELETE ON {TABLE_NAME}\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Run creation facts are append-only');\nEND",
    f"CREATE TRIGGER {TABLE_NAME}_no_duplicate BEFORE INSERT ON {TABLE_NAME}\n"
    f"WHEN EXISTS (SELECT 1 FROM {TABLE_NAME} "
    "WHERE fact_id = NEW.fact_id OR run_id = NEW.run_id)\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Run creation fact conflict');\nEND",
)


def _selector_values(*, project_id: str, session_id: str, source_task_id: str) -> tuple[str, str, str]:
    project_id = _task._text(project_id)
    session_id = _task._text(session_id)
    source_task_id = _task._text(source_task_id)
    _task._project_id(project_id)
    return project_id, session_id, source_task_id


def _source_values(*, project_id: str, session_id: str, run_id: str, source_task_id: str) -> tuple[str, str, str, str]:
    project_id, session_id, source_task_id = _selector_values(
        project_id=project_id, session_id=session_id, source_task_id=source_task_id,
    )
    return _task._text(run_id), project_id, session_id, source_task_id


def validate_order_run_selector(*, project_id: str, session_id: str, source_task_id: str) -> None:
    """Validate raw private selection before a genuine Run ID exists."""
    failed = False
    try:
        _selector_values(project_id=project_id, session_id=session_id, source_task_id=source_task_id)
    except Exception:
        failed = True
    if failed:
        raise OrderRunOriginError("invalid_source") from None


def validate_order_run_source(*, project_id: str, session_id: str, run_id: str, source_task_id: str) -> None:
    """Validate exact identities before Store initialization or write effects."""
    failed = False
    try:
        _source_values(project_id=project_id, session_id=session_id, run_id=run_id, source_task_id=source_task_id)
    except Exception:
        failed = True
    if failed:
        raise OrderRunOriginError("invalid_source") from None


def _failure_code(error: Exception) -> str:
    if type(error) is OrderRunOriginError or type(error) is _task.OrderTaskOriginError:
        return error.code
    return "unavailable"


def _schema(conn: sqlite3.Connection) -> None:
    for (kind, name), expected in zip(_SCHEMA_OBJECTS, SCHEMA_STATEMENTS):
        metadata = _task._rows(
            conn, "SELECT typeof(sql), length(CAST(sql AS BLOB)) FROM sqlite_master "
            "WHERE type = ? AND name = ? LIMIT 2", (kind, name),
        )
        if not metadata:
            raise OrderRunOriginError("unavailable" if kind == "table" else "invalid_source")
        if (len(metadata) != 1 or len(metadata[0]) != 2 or metadata[0][0] != "text"
                or type(metadata[0][1]) is not int or not 0 < metadata[0][1] <= _MAX_SCHEMA_BYTES):
            raise OrderRunOriginError("invalid_source")
        rows = _task._rows(
            conn, "SELECT substr(CAST(sql AS BLOB), 1, ?) FROM sqlite_master "
            "WHERE type = ? AND name = ? LIMIT 2", (_MAX_SCHEMA_BYTES + 1, kind, name),
        )
        if (len(rows) != 1 or len(rows[0]) != 1 or type(rows[0][0]) is not bytes
                or len(rows[0][0]) != metadata[0][1]):
            raise OrderRunOriginError("invalid_source")
        actual = _task._decode(rows[0][0])
        if _task._normalized_schema(actual) != _task._normalized_schema(expected):
            raise OrderRunOriginError("invalid_source")


def _record(conn: sqlite3.Connection, *, table: str, columns: tuple[str, ...], key: str, value: str) -> tuple[str, ...] | None:
    # Every table/column/key below comes from a fixed internal call, never input.
    metadata_columns = ", ".join(
        f"typeof({column}), length(CAST({column} AS BLOB))" for column in columns
    )
    metadata = _task._rows(
        conn, f"SELECT {metadata_columns} FROM {table} WHERE {key} = ? LIMIT 2", (value,),
    )
    if not metadata:
        return None
    if len(metadata) != 1 or len(metadata[0]) != len(columns) * 2:
        raise OrderRunOriginError("invalid_source")
    sizes = []
    for offset in range(0, len(metadata[0]), 2):
        kind, size = metadata[0][offset:offset + 2]
        if kind != "text" or type(size) is not int or not 0 <= size <= _MAX_ID_BYTES:
            raise OrderRunOriginError("invalid_source")
        sizes.append(size)
    selections = ", ".join(
        f"substr(CAST({column} AS BLOB), 1, {_MAX_ID_BYTES + 1})" for column in columns
    )
    rows = _task._rows(
        conn, f"SELECT {selections} FROM {table} WHERE {key} = ? LIMIT 2", (value,),
    )
    if len(rows) != 1 or len(rows[0]) != len(columns):
        raise OrderRunOriginError("invalid_source")
    result = []
    for raw, size in zip(rows[0], sizes):
        if type(raw) is not bytes or len(raw) != size:
            raise OrderRunOriginError("invalid_source")
        result.append(_task._decode(raw))
    return tuple(result)


def _prepare(conn: sqlite3.Connection, *, project_id: str, session_id: str, run_id: str, source_task_id: str) -> PreparedOrderRunSource:
    run_id, project_id, session_id, source_task_id = _source_values(
        project_id=project_id, session_id=session_id, run_id=run_id, source_task_id=source_task_id,
    )
    _task._connection(conn, write=True)
    _task._project_connection(conn, project_id, write=True)
    _schema(conn)
    task_fact = _task.read_order_task_creation(conn, project_id=project_id, task_id=source_task_id)
    if task_fact is None:
        raise OrderRunOriginError("unavailable")
    if type(task_fact) is not _task.OrderTaskCreationFact:
        raise OrderRunOriginError("invalid_source")
    task = _record(conn, table="tasks", columns=("id", "project_id", "session_id"), key="id", value=source_task_id)
    if task != (source_task_id, project_id, session_id):
        raise OrderRunOriginError("invalid_source")
    duplicate_source = _task._rows(
        conn, f"SELECT 1 FROM {TABLE_NAME} WHERE project_id = ? AND source_task_id = ? LIMIT 2",
        (project_id, source_task_id),
    )
    if duplicate_source:
        raise OrderRunOriginError("conflict")
    return PreparedOrderRunSource(run_id, project_id, session_id, source_task_id, _task._digest(task_fact.fact_id))


def prepare_order_run_creation(conn: sqlite3.Connection, *, project_id: str, session_id: str, run_id: str, source_task_id: str) -> PreparedOrderRunSource:
    """Read original Task fact/current identity inside the owned transaction.

    Neither a transaction nor this caller-constructible observation proves
    original order-reader provenance, permissions or a complete Run attempt.
    """
    result = None
    failure = None
    try:
        result = _prepare(conn, project_id=project_id, session_id=session_id, run_id=run_id, source_task_id=source_task_id)
    except Exception as error:
        failure = _failure_code(error)
    if failure is not None:
        raise OrderRunOriginError(failure) from None
    return result


def _fingerprint(values: tuple[str, ...]) -> str:
    raw = json.dumps(
        [FINGERPRINT_VERSION, *values], ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _append(conn: sqlite3.Connection, *, source: PreparedOrderRunSource) -> OrderRunCreationFact:
    if type(source) is not PreparedOrderRunSource:
        raise OrderRunOriginError("invalid_source")
    values = (*_source_values(project_id=source.project_id, session_id=source.session_id,
                             run_id=source.run_id, source_task_id=source.source_task_id),
              _task._digest(source.source_task_fact_id))
    current = _prepare(conn, project_id=source.project_id, session_id=source.session_id,
                       run_id=source.run_id, source_task_id=source.source_task_id)
    if current != source:
        raise OrderRunOriginError("invalid_source")
    run = _record(conn, table="delegation_runs", columns=("run_id", "project_id", "session_id"),
                  key="run_id", value=source.run_id)
    if run != (source.run_id, source.project_id, source.session_id):
        raise OrderRunOriginError("invalid_source")
    fact_id = _fingerprint(values)
    duplicate = _task._rows(
        conn, f"SELECT 1 FROM {TABLE_NAME} WHERE fact_id = ? OR run_id = ? LIMIT 2",
        (fact_id, source.run_id),
    )
    if duplicate:
        raise OrderRunOriginError("conflict")
    cursor = conn.execute(
        f"INSERT INTO {TABLE_NAME} ({', '.join(FACT_COLUMNS)}) VALUES (?, ?, ?, ?, ?, ?)",
        (fact_id, *values),
    )
    try:
        if cursor.rowcount != 1:
            raise OrderRunOriginError("unavailable")
    finally:
        cursor.close()
    return OrderRunCreationFact(fact_id, *values)


def append_order_run_creation(conn: sqlite3.Connection, *, source: PreparedOrderRunSource) -> OrderRunCreationFact:
    """Append after strict Run INSERT on the SAME conn; never own its lifecycle."""
    result = None
    failure = None
    try:
        result = _append(conn, source=source)
    except Exception as error:
        failure = _failure_code(error)
    if failure is not None:
        raise OrderRunOriginError(failure) from None
    return result


def _read(conn: sqlite3.Connection, *, project_id: str, run_id: str) -> OrderRunCreationFact | None:
    project_id, run_id = _task._text(project_id), _task._text(run_id)
    _task._connection(conn, write=False)
    _task._project_connection(conn, project_id)
    _schema(conn)
    row = _record(conn, table=TABLE_NAME, columns=FACT_COLUMNS, key="run_id", value=run_id)
    if row is None:
        return None
    fact_id = _task._digest(row[0])
    values = (*_source_values(project_id=row[2], session_id=row[3], run_id=row[1], source_task_id=row[4]),
              _task._digest(row[5]))
    if row[1] != run_id or row[2] != project_id or fact_id != _fingerprint(values):
        raise OrderRunOriginError("invalid_source")
    task_fact = _task.read_order_task_creation(conn, project_id=project_id, task_id=row[4])
    if task_fact is None or type(task_fact) is not _task.OrderTaskCreationFact or task_fact.fact_id != row[5]:
        raise OrderRunOriginError("invalid_source")
    return OrderRunCreationFact(fact_id, *values)


def read_order_run_creation(conn: sqlite3.Connection, *, project_id: str, run_id: str) -> OrderRunCreationFact | None:
    """Bounded immutable fact/linkage read, not current existence or permission."""
    result = None
    failure = None
    try:
        result = _read(conn, project_id=project_id, run_id=run_id)
    except Exception as error:
        failure = _failure_code(error)
    if failure is not None:
        raise OrderRunOriginError(failure) from None
    return result


def _read_first(conn: sqlite3.Connection, *, project_id: str, source_task_id: str) -> OrderRunCreationFact | None:
    project_id, source_task_id = _task._text(project_id), _task._text(source_task_id)
    _task._connection(conn, write=False)
    _task._project_connection(conn, project_id)
    _schema(conn)
    task_fact = _task.read_order_task_creation(conn, project_id=project_id, task_id=source_task_id)
    if task_fact is None:
        raise OrderRunOriginError("unavailable")
    if type(task_fact) is not _task.OrderTaskCreationFact:
        raise OrderRunOriginError("invalid_source")
    rows = _task._rows(
        conn, f"SELECT run_id FROM {TABLE_NAME} WHERE project_id = ? AND source_task_id = ? LIMIT 2",
        (project_id, source_task_id),
    )
    if not rows:
        return None
    if len(rows) != 1 or type(rows[0]) is not tuple or len(rows[0]) != 1:
        raise OrderRunOriginError("invalid_source")
    run_id = _task._text(rows[0][0])
    fact = _read(conn, project_id=project_id, run_id=run_id)
    if (type(fact) is not OrderRunCreationFact or fact.run_id != run_id
            or fact.project_id != project_id or fact.source_task_id != source_task_id
            or fact.source_task_fact_id != task_fact.fact_id):
        raise OrderRunOriginError("invalid_source")
    return fact


def read_order_first_run_creation(conn: sqlite3.Connection, *, project_id: str, source_task_id: str) -> OrderRunCreationFact | None:
    """Read retained creation history, not current existence or execution status."""
    result = None
    failure = None
    try:
        result = _read_first(conn, project_id=project_id, source_task_id=source_task_id)
    except Exception as error:
        failure = _failure_code(error)
    if failure is not None:
        raise OrderRunOriginError(failure) from None
    return result
